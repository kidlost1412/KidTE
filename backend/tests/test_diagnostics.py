import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import diagnostics


def test_diagnostics_never_returns_api_key(monkeypatch):
    monkeypatch.setattr(diagnostics, "DEEPSEEK_API_KEY", "sk-secret-value")
    monkeypatch.setattr(diagnostics, "_webview2_version", lambda: "123.0")
    result = diagnostics.run_setup_checks(include_audio=False)
    rendered = str(result)
    assert "sk-secret-value" not in rendered
    assert "giá trị được che" in rendered


def test_writable_check_cleans_its_probe(tmp_path):
    result = diagnostics._writable_status(tmp_path)
    assert result["status"] == "ok"
    assert not list(tmp_path.glob(".write-check-*"))


def test_missing_ffmpeg_points_to_video_tools_update(monkeypatch):
    monkeypatch.setattr(diagnostics, "find_tool", lambda _name: None)
    report = diagnostics.run_setup_checks(include_audio=False)
    check = next(row for row in report["checks"] if row["id"] == "ffmpeg")
    assert check["status"] == "warn"
    assert check["fix"]["text"] == "Mở Cài đặt → Cập nhật → Công cụ video để cài hoặc cập nhật gói."
    assert check["fix"]["page"] == "system"
    assert check["fix"]["sub"] == "updates"


def test_diagnostics_reports_broken_media_tools_without_failing(monkeypatch):
    def broken(_name):
        raise RuntimeError("active_component_invalid")

    monkeypatch.setattr(diagnostics, "find_tool", broken)
    report = diagnostics.run_setup_checks(include_audio=False)
    check = next(row for row in report["checks"] if row["id"] == "ffmpeg")
    assert check["status"] == "warn"
    assert check["detail"] == "active_component_invalid"
