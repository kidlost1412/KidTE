import sys
from pathlib import Path
from types import SimpleNamespace


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import windows_integration


def test_open_extension_setup_opens_exact_folder_and_chrome(tmp_path, monkeypatch):
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text("{}", encoding="utf-8")
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    monkeypatch.setattr(windows_integration, "chrome_executable", lambda: chrome)
    opened, commands = [], []
    result = windows_integration.open_extension_setup(
        extension, opener=opened.append, popen=lambda command, **kwargs: commands.append((command, kwargs))
    )
    assert result["ok"] is True
    assert opened == [str(extension)]
    assert commands == [([str(chrome), "--new-tab", "chrome://extensions/"], {"shell": False})]
    assert result["opened"] == {"chrome_page": True, "folder": True}


def test_open_extension_setup_reports_partial_windows_failure(tmp_path, monkeypatch):
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text("{}", encoding="utf-8")
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"")
    monkeypatch.setattr(windows_integration, "chrome_executable", lambda: chrome)
    result = windows_integration.open_extension_setup(
        extension,
        opener=lambda _path: (_ for _ in ()).throw(OSError("blocked")),
        popen=lambda *_args, **_kwargs: object(),
    )
    assert result["ok"] is False
    assert result["opened"] == {"chrome_page": True, "folder": False}
    assert result["error"] == "folder:OSError"


def test_open_extension_setup_refuses_wrong_folder(tmp_path):
    assert windows_integration.open_extension_setup(tmp_path)["error"] == "extension_folder_missing"


def test_focus_failure_is_reported_without_raising(monkeypatch):
    hwnd = 123
    fake_gui = SimpleNamespace(
        EnumWindows=lambda callback, arg: callback(hwnd, arg),
        GetWindowText=lambda _hwnd: "Kid TE — Trợ lý vận hành LIVE",
        IsIconic=lambda _hwnd: False,
        SetForegroundWindow=lambda _hwnd: (_ for _ in ()).throw(OSError("denied")),
        ShowWindow=lambda *_args: None,
        FlashWindow=lambda *_args: None,
    )
    fake_process = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd: (1, 99))
    monkeypatch.setattr(windows_integration.os, "getpid", lambda: 99)
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_process)
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(SW_RESTORE=9, SW_SHOW=5))

    assert windows_integration.focus_current_process_window() is False
