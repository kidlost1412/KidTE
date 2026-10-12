"""Mọi tiến trình con phải chạy ẩn khi app không có console."""
import subprocess
import sys

import pytest

import console_io


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_children_get_no_window_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess.Popen, "__init__", lambda self, *a, **k: seen.append(k.get("creationflags", 0)))
    monkeypatch.delattr(subprocess.Popen, "_kidte_no_window", raising=False)
    console_io.hide_child_consoles()
    subprocess.Popen(["tasklist"])
    subprocess.Popen(["x"], creationflags=subprocess.CREATE_NEW_CONSOLE)
    assert seen[0] & subprocess.CREATE_NO_WINDOW
    assert seen[1] == subprocess.CREATE_NEW_CONSOLE
    console_io.hide_child_consoles()  # gọi lại không bọc lần hai
