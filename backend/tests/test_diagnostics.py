import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("worker_state", ["available", "missing", "damaged"])
def test_frozen_transcription_diagnostics_checks_optional_worker_not_core_module(
        tmp_path, monkeypatch, worker_state):
    import component_runtime

    worker = tmp_path / "KidTETranscription.exe"
    worker.write_bytes(b"signed worker fixture")
    monkeypatch.setattr(diagnostics, "IS_FROZEN", True)
    # The core intentionally excludes faster_whisper; the installed optional
    # worker owns that dependency in packaged mode.
    real_find_spec = diagnostics.importlib.util.find_spec
    monkeypatch.setattr(
        diagnostics.importlib.util, "find_spec",
        lambda name: None if name == "faster_whisper" else real_find_spec(name))
    if worker_state == "available":
        monkeypatch.setattr(
            component_runtime, "component_path",
            lambda component_id, relative_path: worker
            if (component_id, relative_path) == ("video-analysis-cpu", "KidTETranscription.exe")
            else None)
    elif worker_state == "missing":
        monkeypatch.setattr(component_runtime, "component_path", lambda *_args: None)
    else:
        def damaged(*_args):
            raise RuntimeError("component resource is missing or damaged")

        monkeypatch.setattr(component_runtime, "component_path", damaged)

    report = diagnostics.run_setup_checks(include_audio=False)
    check = next(row for row in report["checks"] if row["id"] == "module-faster_whisper")
    assert check["label"] == "Bộ chép lời trên máy"
    if worker_state == "available":
        assert check["status"] == "ok"
        assert check["detail"] == str(worker)
        assert "fix" not in check
    else:
        assert check["status"] in {"warn", "error"}
        assert check["fix"]["page"] == "system"
        assert check["fix"]["sub"] == "updates"
        assert "Bộ chép lời trên máy" in check["fix"]["text"]
        if worker_state == "damaged":
            assert "missing or damaged" in check["detail"]
        else:
            assert check["status"] == "warn"


def test_source_transcription_diagnostics_still_checks_python_module(monkeypatch):
    import component_runtime

    monkeypatch.setattr(diagnostics, "IS_FROZEN", False)
    monkeypatch.setattr(diagnostics.importlib.util, "find_spec",
                        lambda _name: None)

    def unexpected_component_lookup(*_args):
        raise AssertionError("source mode should inspect the Python environment")

    monkeypatch.setattr(component_runtime, "component_path", unexpected_component_lookup)
    report = diagnostics.run_setup_checks(include_audio=False)
    check = next(row for row in report["checks"] if row["id"] == "module-faster_whisper")
    assert check["status"] == "warn"
    assert check["fix"]["text"].startswith("Chạy:")
