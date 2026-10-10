"""Tự học lại không được tự gọi model lặp lại khi đề xuất trước chưa được duyệt."""
from datetime import datetime, timedelta

import learning


class _Store:
    def __init__(self, relearns, distills, last="2026-09-23T21:40:57+07:00"):
        self._runs = {"relearn": relearns, "distill": distills}
        self._last = last

    def settings(self):
        return {"auto_relearn": True}

    def meta(self, key, default=""):
        return self._last if key == "last_relearn_at" else default

    def runs(self, kind, limit=20):
        return list(self._runs.get(kind, []))[:limit]


def _iso(hours_ago):
    return (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _learner(store, submitted):
    lr = learning.Learner(store, responder_provider=lambda: None, catalog_provider=lambda: [],
                          activity_provider=lambda: None)
    lr.submit_relearn = lambda trigger="manual", sync=False: submitted.append(trigger) or "new"
    return lr


DISTILLS = [{"created_at": _iso(h), "status": "applied"} for h in (40, 39, 38)]


def test_pending_proposal_blocks_auto_relearn():
    sub = []
    store = _Store([{"created_at": _iso(30), "status": "done"}], DISTILLS)
    assert _learner(store, sub).maybe_auto_relearn() is None and sub == []


def test_at_most_once_per_day():
    sub = []
    store = _Store([{"created_at": _iso(2), "status": "discarded"}], DISTILLS + [
        {"created_at": _iso(1), "status": "applied"} for _ in range(3)])
    assert _learner(store, sub).maybe_auto_relearn() is None and sub == []


def test_only_new_teachings_since_last_run_count():
    sub = []
    store = _Store([{"created_at": _iso(30), "status": "applied"}], DISTILLS)   # all older than last run
    assert _learner(store, sub).maybe_auto_relearn() is None and sub == []
    store = _Store([{"created_at": _iso(30), "status": "applied"}],
                   [{"created_at": _iso(h), "status": "applied"} for h in (5, 4, 3)])
    _learner(store, sub).maybe_auto_relearn()
    assert sub == ["auto"]


def test_auto_relearn_is_off_by_default(tmp_path):
    store = learning.LearningStore(tmp_path / "ai.sqlite3")
    try:
        assert store.settings()["auto_relearn"] is False
    finally:
        getattr(store, "close", lambda: None)()
