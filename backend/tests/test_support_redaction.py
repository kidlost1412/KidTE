import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import support_report
from structured_log import redact
from support_report import redact_support


GEMINI = "AIza" + "SyD1234567890abcdefghijklmnopqrstuv"
TELEGRAM = "123456789:" + "AAH" + "x" * 32


def test_known_key_shapes_are_redacted_without_assignment():
    text = redact(f"loaded {GEMINI} and bot {TELEGRAM} ghp_{'a' * 36} AKIAABCDEFGHIJKLMNOP")
    assert GEMINI not in text and TELEGRAM not in text
    assert "ghp_" not in text and "AKIAABCDEFGHIJKLMNOP" not in text


def test_vietnamese_phone_variants_are_redacted():
    for phone in ("0912345678", "0912 345 678", "0912.345.678", "0912-345-678",
                  "+84 912345678", "+84912345678", "84912345678", "+84 912 345 678"):
        assert "[REDACTED_PHONE]" in redact_support(f"khach {phone} hoi"), phone


def test_numbers_that_are_not_phones_survive():
    text = redact_support("latency 0.123456789 s at 2026-10-18T05:17:00Z pid 19656 v1.15.15")
    assert "0.123456789" in text and "2026-10-18" in text and "19656" in text


def test_windows_user_name_is_hidden_in_plain_and_json_paths():
    text = redact_support('File "C:\\Users\\Nguyen Van A\\AppData\\x.py" and "C:\\\\Users\\\\hp\\\\DuLieu"'
                          " and C:/Users/kid/Desktop")
    assert "Nguyen Van A" not in text and "\\\\hp\\\\" not in text and "kid" not in text
    assert "C:\\Users\\<user>\\AppData" in text


def test_report_includes_system_info_and_redacted_log_tails(tmp_path, monkeypatch):
    logs, reports, app = tmp_path / "logs", tmp_path / "reports", tmp_path / "App"
    logs.mkdir()
    app.mkdir()
    (logs / "core.jsonl").write_text("{}\n", encoding="utf-8")
    lines = [f"line {i}" for i in range(1000)]
    lines.append(f'Traceback File "C:\\Users\\Minh\\x.py" key {GEMINI} sdt 0912 345 678')
    (logs / "console.log").write_text("\n".join(lines), encoding="utf-8")
    (app / "launcher-startup.log").write_text("[Kid TE launcher] rollback\n", encoding="utf-8")
    monkeypatch.setattr(support_report, "LOG_DIR", logs)
    monkeypatch.setattr(support_report, "REPORT_DIR", reports)
    monkeypatch.setenv("KIDTE_APP_ROOT", str(app))
    monkeypatch.setattr(support_report, "_generated_documents", lambda *_: {
        "generated/system.json": {**support_report._system_info(), "app_version": "1.15.15"}})
    preview = support_report.preview_support_report()
    assert "logs/console.log.tail.txt" in preview["files"]
    assert "app/launcher-startup.log.tail.txt" in preview["files"]
    result = support_report.create_support_report(now=datetime(2026, 10, 18, tzinfo=timezone.utc))
    with zipfile.ZipFile(result["path"]) as archive:
        console = archive.read("logs/console.log.tail.txt").decode("utf-8")
        launcher = archive.read("app/launcher-startup.log.tail.txt").decode("utf-8")
        system = json.loads(archive.read("generated/system.json"))
    assert "line 0\n" not in console and "line 999" in console
    assert "Minh" not in console and GEMINI not in console and "345 678" not in console
    assert "rollback" in launcher
    assert system["app_version"] == "1.15.15" and "cpu_count" in system
