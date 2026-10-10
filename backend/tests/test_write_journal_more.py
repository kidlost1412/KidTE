"""E04: journal coverage for merge / rollback / video-job draft file writes and WorkspaceStore.close()."""
import json
import threading
from unittest import mock

import pytest

from knowledge import KnowledgeBase
from workspace import WorkspaceStore


class Crash(BaseException):
    """Simulated process death: not caught by `except Exception` cleanup."""


class _Engine:
    def __init__(self, knowledge, video_root=None):
        self.knowledge = knowledge
        self.extension_heartbeat = {}
        self.video_analysis = type('V', (), {'root': video_root})()

    def mapping_state(self):
        return {"products": []}

    def video_analysis_jobs(self):
        return {"jobs": []}

    def current_video_position(self):
        return {"state": "waiting", "video_id": ""}


TIMELINE = {"video_id": "keep", "title": "Giữ", "source_file": "live 2.mp4", "cycle_ms": 60000,
            "repin_every_ms": 20000, "products": {"p": {"bag_index": 1, "include_all": ["p"]}},
            "events": [{"at_ms": 0, "product": "p"}], "script_contexts": []}


@pytest.fixture
def ws(tmp_path):
    cat, prof = tmp_path / 'k.json', tmp_path / 'p.json'
    cat.write_text('{"nhom_san_pham": []}', encoding='utf-8')
    prof.write_text('{"products":{}}', encoding='utf-8')
    videos = tmp_path / 'videos'
    for name in ('keep', 'other'):
        (videos / name).mkdir(parents=True)
        timeline = {**TIMELINE, 'video_id': name, 'title': name}
        if name == 'other':
            timeline['source_sha256'] = 'abc'
            timeline['source_file'] = 'live-2.mp4'
        (videos / name / 'timeline.json').write_text(json.dumps(timeline), encoding='utf-8')

    def listed(check_fingerprint=True):
        return [{"video_id": d.name, "timeline": json.loads((d / 'timeline.json').read_text(encoding='utf-8')),
                 "fingerprint": None, "errors": []} for d in sorted(videos.iterdir()) if (d / 'timeline.json').exists()]
    store = WorkspaceStore(_Engine(KnowledgeBase(cat, prof)), tmp_path)
    with mock.patch('workspace.VIDEO_DIR', videos), mock.patch('workspace.list_videos', side_effect=listed):
        yield store, videos, tmp_path


def _timeline(videos, name='keep'):
    return json.loads((videos / name / 'timeline.json').read_text(encoding='utf-8'))


def _leftovers(videos):
    return [p.name for p in videos.rglob('*.journal.*')]


def test_workspace_close_blocks_new_connections_and_is_idempotent(ws):
    store, _, _ = ws
    with store._db() as db:
        db.execute('SELECT 1')
    store.close()
    store.close()
    assert store.closed is True
    with pytest.raises(RuntimeError, match='workspace_closed'):
        with store._db():
            pass


def test_workspace_close_waits_for_in_flight_connection(ws):
    store, _, _ = ws
    entered, release, done = threading.Event(), threading.Event(), []

    def worker():
        with store._db() as db:
            db.execute('SELECT 1')
            entered.set()
            release.wait(3)
    thread = threading.Thread(target=worker)
    thread.start()
    entered.wait(3)
    closer = threading.Thread(target=lambda: (store.close(), done.append(1)))
    closer.start()
    closer.join(.3)
    assert done == []          # still waiting for the in-flight connection
    release.set()
    thread.join(3)
    closer.join(3)
    assert done == [1]


def test_merge_success_leaves_no_journal_residue(ws):
    store, videos, _ = ws
    result = store.resolve_video_duplicate('keep', {'other_id': 'other', 'action': 'merge'})
    assert result['ok'] is True
    assert _timeline(videos)['source_sha256'] == 'abc'
    assert store.journal.pending() == [] and _leftovers(videos) == []


def test_merge_crash_before_db_commit_rolls_back_timeline(ws):
    store, videos, _ = ws
    original = _timeline(videos)
    real = store._record_change

    def crash_on_merge(*args, **kwargs):
        if kwargs.get('action') == 'merge':
            raise Crash()
        return real(*args, **kwargs)
    with mock.patch.object(store, '_record_change', side_effect=crash_on_merge):
        with pytest.raises(Crash):
            store.resolve_video_duplicate('keep', {'other_id': 'other', 'action': 'merge'})
    assert _timeline(videos).get('source_sha256') == 'abc'       # file already replaced...
    assert len(store.journal.pending()) == 1
    actions = [a for _, a in store.recover_journal()]
    assert actions == ['rolled_back']
    assert _timeline(videos) == original                          # ...restored by recovery
    assert store.journal.pending() == [] and _leftovers(videos) == []
    assert store.recover_journal() == []


def test_merge_crash_after_db_commit_rolls_forward(ws):
    store, videos, _ = ws
    with mock.patch.object(store, '_commit_file_first', side_effect=Crash()):
        with pytest.raises(Crash):
            store.resolve_video_duplicate('keep', {'other_id': 'other', 'action': 'merge'})
    actions = [a for _, a in store.recover_journal()]
    assert actions == ['rolled_forward']
    assert _timeline(videos)['source_sha256'] == 'abc'
    assert _leftovers(videos) == []


def _seed_rollback_history(store, videos):
    older = {**_timeline(videos), 'title': 'Cũ'}
    with store._db() as db:
        db.execute("INSERT INTO operator_changes (change_id,resource,record_id,changed_at,reason,before_json,"
                   "after_json,before_revision,after_revision,action) VALUES "
                   "('c1','videos','keep','2026-10-01T00:00:00','x',?,?,'a','b','update')",
                   (json.dumps({'timeline': older}), json.dumps({})))
    return older


def test_video_rollback_success_and_db_failure_restores_file(ws):
    store, videos, _ = ws
    older = _seed_rollback_history(store, videos)
    current = _timeline(videos)
    with mock.patch.object(store, '_record_change', side_effect=ValueError('db_down')):
        with pytest.raises(ValueError, match='db_down'):
            store.rollback('videos', 'keep')
    assert _timeline(videos) == current                         # file put back when DB write failed
    assert store.journal.pending() == [] and _leftovers(videos) == []
    store.rollback('videos', 'keep')
    assert _timeline(videos)['title'] == older['title']
    assert store.journal.pending() == [] and _leftovers(videos) == []


def test_video_rollback_crash_recovery_both_sides(ws):
    store, videos, _ = ws
    older = _seed_rollback_history(store, videos)
    current = _timeline(videos)
    with mock.patch.object(store, '_record_change', side_effect=Crash()):
        with pytest.raises(Crash):
            store.rollback('videos', 'keep')
    assert [a for _, a in store.recover_journal()] == ['rolled_back']
    assert _timeline(videos) == current
    with mock.patch.object(store, '_commit_file_first', side_effect=Crash()):
        with pytest.raises(Crash):
            store.rollback('videos', 'keep')
    assert [a for _, a in store.recover_journal()] == ['rolled_forward']
    assert _timeline(videos)['title'] == older['title']


def test_job_draft_helpers_recover_with_change_revision(ws):
    store, _, tmp_path = ws
    path = tmp_path / 'jobs' / 'j1' / 'draft.json'
    path.parent.mkdir(parents=True)
    path.write_text('{"v": 1}', encoding='utf-8')
    op, prev = store._begin_file_first('video_job_draft', path, {'v': 2}, 'video-jobs', 'j1')
    assert json.loads(path.read_text()) == {'v': 2}
    assert store.recover_journal() and json.loads(path.read_text()) == {'v': 1}   # DB never moved
    op, prev = store._begin_file_first('video_job_draft', path, {'v': 2}, 'video-jobs', 'j1')
    store._record_change('video-jobs', 'j1', {'v': 1}, {'v': 2}, 'r')             # DB committed, then crash
    assert [a for _, a in store.recover_journal()] == ['rolled_forward']
    assert json.loads(path.read_text()) == {'v': 2}
    assert not list(path.parent.glob('*.journal.*'))
