import json

import pytest

from write_journal import WriteJournal


def _setup(tmp_path, committed):
    target = tmp_path / "timeline.json"
    target.write_text('{"old": 1}', encoding="utf-8")
    tmp = tmp_path / "timeline.json.journal.tmp"
    tmp.write_text('{"new": 1}', encoding="utf-8")
    journal = WriteJournal(tmp_path / "journal.jsonl")
    journal.begin("video_timeline", [(target, tmp)], "v1", 0)
    return journal, target, tmp, (lambda key: 1 if committed else 0)


def test_crash_after_db_before_file_recovers(tmp_path):
    journal, target, tmp, rev = _setup(tmp_path, True)
    assert [a for _, a in journal.recover(rev)] == ["rolled_forward"]
    assert json.loads(target.read_text()) == {"new": 1}
    assert not tmp.exists()
    assert journal.recover(rev) == []  # idempotent


def test_crash_before_db_rolls_back(tmp_path):
    journal, target, tmp, rev = _setup(tmp_path, False)
    assert [a for _, a in journal.recover(rev)] == ["rolled_back"]
    assert json.loads(target.read_text()) == {"old": 1}
    assert not tmp.exists()
    assert journal.recover(rev) == []


def test_torn_journal_line_ignored(tmp_path):
    journal, target, tmp, rev = _setup(tmp_path, False)
    with open(journal.path, "a", encoding="utf-8") as h:
        h.write('{"op_id": "x", "event": "int')
    assert len(journal.recover(rev)) == 1


def test_409_on_revision_conflict(tmp_path):
    from test_workspace import FakeEngine
    from knowledge import KnowledgeBase
    from workspace import WorkspaceStore
    cat = tmp_path / "k.json"
    prof = tmp_path / "p.json"
    cat.write_text('{"nhom_san_pham": []}', encoding="utf-8")
    prof.write_text('{"products":{}}', encoding="utf-8")
    store = WorkspaceStore(FakeEngine(KnowledgeBase(cat, prof)), tmp_path)
    from unittest import mock
    folder = tmp_path / "videos" / "v1"
    folder.mkdir(parents=True)
    tl = {"video_id": "v1", "title": "V", "cycle_ms": 60000, "repin_every_ms": 20000,
          "products": {}, "events": [], "script_contexts": []}
    (folder / "timeline.json").write_text(json.dumps(tl), encoding="utf-8")
    listed = [{"video_id": "v1", "timeline": tl, "fingerprint": None, "errors": []}]
    draft = {"events": [], "script_contexts": []}
    with mock.patch("workspace.list_videos", return_value=listed),             mock.patch("workspace.VIDEO_DIR", tmp_path / "videos"):
        first = store.save_video_draft("v1", {"draft": draft, "expected_draft_revision": 0})
        with pytest.raises(ValueError, match="revision_conflict"):
            store.save_video_draft("v1", {"draft": draft, "expected_draft_revision": 0})
    assert first["draft_revision"] == 1
    assert store.recover_journal() == []
