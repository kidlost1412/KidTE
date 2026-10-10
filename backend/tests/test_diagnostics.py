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
