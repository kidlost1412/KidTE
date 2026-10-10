"""Offline tests for the standalone CAPTCHA package bootstrap."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

PROJECT = Path(__file__).resolve().parents[2]
ENTRY_PATH = PROJECT / "packaging" / "captcha_entry.py"
sys.path.insert(0, str(PROJECT / "backend" / "src"))
spec = importlib.util.spec_from_file_location("kidte_captcha_entry_test", ENTRY_PATH)
captcha_entry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = captcha_entry
spec.loader.exec_module(captcha_entry)


def _solver_executable(app_root: Path) -> Path:
    return app_root / "packages" / "captcha-solver" / "1.0.0" / "Captcha" / "KidTECaptcha.exe"


def test_ffmpeg_resolves_from_derived_app_root_after_uac_loses_environment(tmp_path, monkeypatch):
    app_root = tmp_path / "Installed"
    executable = _solver_executable(app_root)
    ffmpeg = app_root / "packages" / "media-tools" / "1.0.0" / "tools" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.write_bytes(b"selected ffmpeg")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delenv("KIDTE_APP_ROOT", raising=False)
    monkeypatch.delenv("KIDTE_FFMPEG", raising=False)

    def resolve(component_id, relative_path):
        assert component_id == "media-tools"
        assert relative_path == "tools/ffmpeg.exe"
        assert Path(os.environ["KIDTE_APP_ROOT"]) == app_root
        return ffmpeg

    with mock.patch.dict(sys.modules, {"component_runtime": SimpleNamespace(component_path=resolve)}):
        assert captcha_entry._ffmpeg_binary() == ffmpeg


def test_selected_damaged_media_package_does_not_fall_back_to_legacy(tmp_path, monkeypatch):
    app_root = tmp_path / "Installed"
    executable = _solver_executable(app_root)
    legacy = app_root / "tools" / "ffmpeg.exe"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy copy")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delenv("KIDTE_APP_ROOT", raising=False)
    monkeypatch.delenv("KIDTE_FFMPEG", raising=False)
    resolver = SimpleNamespace(component_path=mock.Mock(
        side_effect=RuntimeError("component resource digest mismatch")))

    with (mock.patch.dict(sys.modules, {"component_runtime": resolver}),
          pytest.raises(RuntimeError, match="digest mismatch")):
        captcha_entry._ffmpeg_binary()
    resolver.component_path.assert_called_once_with("media-tools", "tools/ffmpeg.exe")


def test_mismatched_inherited_app_root_is_rejected(tmp_path, monkeypatch):
    derived_root = tmp_path / "SelectedInstall"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(_solver_executable(derived_root)))
    monkeypatch.setenv("KIDTE_APP_ROOT", str(tmp_path / "OtherInstall"))

    with pytest.raises(RuntimeError, match="captcha_app_root_mismatch"):
        captcha_entry._initialize_app_root()


def test_explicit_ffmpeg_path_remains_supported(tmp_path, monkeypatch):
    ffmpeg = tmp_path / "custom-ffmpeg.exe"
    ffmpeg.write_bytes(b"custom ffmpeg")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("KIDTE_APP_ROOT", raising=False)
    monkeypatch.setenv("KIDTE_FFMPEG", str(ffmpeg))

    assert captcha_entry._ffmpeg_binary() == ffmpeg


def test_source_mode_keeps_legacy_ffmpeg_fallback(tmp_path, monkeypatch):
    project = tmp_path / "source-project"
    entry = project / "packaging" / "captcha_entry.py"
    entry.parent.mkdir(parents=True)
    legacy = project / "tools" / "ffmpeg" / "ffmpeg.exe"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"source ffmpeg")
    monkeypatch.setattr(captcha_entry, "__file__", str(entry))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("KIDTE_APP_ROOT", raising=False)
    monkeypatch.delenv("KIDTE_FFMPEG", raising=False)

    assert captcha_entry._ffmpeg_binary() == legacy


def test_frozen_legacy_install_keeps_legacy_ffmpeg_fallback(tmp_path, monkeypatch):
    app_root = tmp_path / "LegacyInstall"
    executable = app_root / "Captcha" / "KidTECaptcha.exe"
    legacy = app_root / "tools" / "ffmpeg.exe"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy ffmpeg")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delenv("KIDTE_APP_ROOT", raising=False)
    monkeypatch.delenv("KIDTE_FFMPEG", raising=False)
    resolver = SimpleNamespace(component_path=mock.Mock(return_value=None))

    with mock.patch.dict(sys.modules, {"component_runtime": resolver}):
        assert captcha_entry._ffmpeg_binary() == legacy
    resolver.component_path.assert_called_once_with("media-tools", "tools/ffmpeg.exe")


def test_package_probe_is_json_and_does_not_require_data_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(ENTRY_PATH), "--package-probe"])
    monkeypatch.setattr(captcha_entry, "_resource_count", lambda: 3)
    monkeypatch.setattr(captcha_entry, "_ffmpeg_binary", lambda: tmp_path / "ffmpeg.exe")
    monkeypatch.setattr(captcha_entry, "_single_instance",
                        lambda: (_ for _ in ()).throw(AssertionError("solver startup reached")))
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert captcha_entry.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["resource_count"] == 3
    assert report["ffmpeg"] == str(tmp_path / "ffmpeg.exe")
