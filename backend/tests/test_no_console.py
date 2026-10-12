"""Khong bao gio hien console: spec windowed + luong chuan an toan khi None."""
import re
import sys
from pathlib import Path

import console_io

PACKAGING = Path(__file__).resolve().parents[2] / "packaging"


def test_user_facing_specs_are_windowed():
    core = (PACKAGING / "KidTE.spec").read_text(encoding="utf-8")
    assert re.search(r'name="KidTE",\s*console=False', core)
    for name in ("Updater.spec", "KidTECaptcha.spec"):
        assert "console=False" in (PACKAGING / name).read_text(encoding="utf-8")


def test_ensure_std_streams_when_none(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log = tmp_path / "logs" / "console.log"
    console_io.ensure_std_streams(log)
    print("xin chao")
    sys.stderr.write("loi\n")
    console_io.force_utf8_console()
    sys.stdout.flush()
    assert "xin chao" in log.read_text(encoding="utf-8")


def test_ensure_std_streams_null_fallback(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    console_io.ensure_std_streams(None)
    print("khong loi")
