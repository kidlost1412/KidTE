"""Lỗi bỏ qua có chủ đích phải để lại dấu vết trong log (không nuốt im), và không spam."""
import logging
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import swallowed  # noqa: E402


def test_note_logs_with_event_and_once_per_key(caplog):
    caplog.set_level(logging.DEBUG, logger="kid-te.core")
    try:
        raise ValueError("boom")
    except ValueError:
        for _ in range(5):
            swallowed.note("unit_test_swallow", once=True, key="a")
        swallowed.note("unit_test_swallow", once=True, key="b")
    rows = [r for r in caplog.records if getattr(r, "event", "") == "unit_test_swallow"]
    assert len(rows) == 2 and rows[0].exc_info


def test_note_never_raises(monkeypatch):
    monkeypatch.setattr(swallowed, "_LOGGER", None)
    swallowed.note("x")            # logger hỏng vẫn không ném lỗi


def test_pdf_page_failure_is_logged_and_skipped(caplog, monkeypatch):
    import documents

    class Page:
        def __init__(self, ok):
            self.ok = ok

        def extract_text(self):
            if not self.ok:
                raise RuntimeError("bad page")
            return "trang tốt " * 50

    class Reader:
        def __init__(self, _):
            self.pages = [Page(True), Page(False)]

    import types
    fake = types.ModuleType("pypdf")
    fake.PdfReader = Reader
    monkeypatch.setitem(sys.modules, "pypdf", fake)
    caplog.set_level(logging.WARNING, logger="kid-te.core")
    fn = documents._pdf_text
    text = fn(b"%PDF")
    assert "trang tốt" in (text if isinstance(text, str) else str(text))
    assert any(getattr(r, "event", "") == "pdf_page_extract_failed" for r in caplog.records)
