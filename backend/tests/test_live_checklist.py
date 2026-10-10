import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import live_checklist as lc


def test_step_definitions():
    ids = [s["id"] for s in lc.STEPS]
    assert len(ids) == len(set(ids)) >= 14
    for s in lc.STEPS:
        assert s["instructions"] and s["observe"]
        assert all(p.startswith("/api/") for p in s["api"])
    assert "privacy_forget" in ids and "queue_mode" in ids
    assert "[queue_mode]" in lc.list_steps()


def test_result_writer_and_resume(tmp_path):
    st = lc.new_state()
    lc.record(st, "send_real", "pass", "ok | x")
    out = tmp_path / "a" / "t.json"
    j, md = lc.save(st, out)
    assert json.loads(j.read_text("utf-8"))["results"]["send_real"]["result"] == "pass"
    assert "chua lam" in md.read_text("utf-8")
    answers = iter(["f", "loi"] * 20)
    st2 = lc.run(json.loads(j.read_text("utf-8")), out, tmp_path,
                 ask=lambda _: next(answers), say=lambda *_: None)
    assert st2["results"]["send_real"]["result"] == "pass"
    assert len(st2["results"]) == len(lc.STEPS)


def test_only_get_requests(monkeypatch, tmp_path):
    seen = []

    class R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake(req, timeout=None):
        seen.append(req)
        return R()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    answers = iter(["p", ""] * 20)
    lc.run(lc.new_state(), tmp_path / "r.json", tmp_path,
           ask=lambda _: next(answers), say=lambda *_: None)
    assert seen
    for r in seen:
        assert r.get_method() == "GET" and r.data is None
        assert r.full_url.startswith("http://127.0.0.1:8793/api/")


def test_rejects_non_api_path():
    with pytest.raises(ValueError):
        lc.http_get("http://evil/x")
