"""O2 faulthandler/excepthook và O6 console.log xoay vòng."""
import sys
import threading
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import console_io
import crash_log


def test_console_log_rotates_with_one_shared_handle(tmp_path):
    path = tmp_path / "console.log"
    stream = console_io.RotatingTextStream(path, max_bytes=100, backups=3)
    for index in range(40):
        stream.write(f"dòng {index:03d} " + "x" * 10 + "\n")
    stream.flush()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["console.log", "console.log.1", "console.log.2", "console.log.3"]
    newest = path.read_text(encoding="utf-8") + (tmp_path / "console.log.1").read_text(encoding="utf-8")
    assert "dòng 039" in newest
    assert all(p.stat().st_size < 200 for p in tmp_path.iterdir())


def test_ensure_std_streams_shares_rotating_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    console_io.ensure_std_streams(tmp_path / "logs" / "console.log")
    assert sys.stdout is sys.stderr
    assert isinstance(sys.stdout, console_io.RotatingTextStream)
    sys.stdout._stream.close()


def test_excepthooks_write_traceback_to_crash_log_and_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(crash_log, "_state", {"stream": None, "installed": False})
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    logger = mock.Mock()
    with mock.patch.object(crash_log.faulthandler, "enable") as enable:
        path = crash_log.install(tmp_path, logger)
    enable.assert_called_once()
    try:
        raise RuntimeError("boom in main")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    worker = threading.Thread(target=lambda: 1 / 0, name="worker-x")
    worker.start()
    worker.join()
    text = path.read_text(encoding="utf-8")
    assert "boom in main" in text and "ZeroDivisionError" in text and "worker-x" in text
    events = [call.kwargs["extra"]["event"] for call in logger.critical.call_args_list]
    assert events == ["unhandled_exception", "unhandled_exception"]
    crash_log._state["stream"].close()
