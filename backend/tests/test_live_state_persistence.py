import json
import os

from live_session import VideoLock
import live_state_store as store

REF = {'video_id': 'v1', 'duration_s': 100.0}


def _locked(clock):
    lock = VideoLock(clock=clock)
    lock.observe(REF, 10, .5)
    clock.t += 9
    assert lock.observe(REF, 19, .5)
    return lock


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _entry(lock, rev='r1', at=5000.0):
    return {'room_id': 'x', 'locked': True, 'locked_at': at, 'listen_manual': True,
            'lock': lock.export(rev)}


def test_lock_survives_restart(tmp_path):
    p = tmp_path / 'live_state.json'
    lock = _locked(Clock())
    store.save_platform('tiktok', _entry(lock), p)
    saved = store.fresh_lock(store.get_platform('tiktok', p), 'r1', now=5100.0)
    assert saved and saved['video_id'] == 'v1'
    new = VideoLock(clock=Clock())
    assert new.restore(saved, REF)
    assert new.state == 'uncertain'          # never locked without re-verification
    assert new.snapshot()['available'] is False
    assert new.next_check <= new.clock()
    assert store.get_platform('tiktok', p)['listen_manual'] is True


def test_stale_or_other_library_not_restored(tmp_path):
    p = tmp_path / 's.json'
    store.save_platform('tiktok', _entry(_locked(Clock())), p)
    e = store.get_platform('tiktok', p)
    assert store.fresh_lock(e, 'r1', now=5000 + 601) is None
    assert store.fresh_lock(e, 'r2', now=5100) is None
    assert store.fresh_lock({'locked': True, 'locked_at': 'x', 'lock': {}}, 'r1') is None


def test_restore_rejects_mismatched_video():
    assert not VideoLock().restore({'video_id': 'v1'}, {'video_id': 'v2', 'duration_s': 5})


def test_corrupt_state_file_ignored(tmp_path):
    p = tmp_path / 'c.json'
    p.write_text('{not json', encoding='utf-8')
    assert store.load(p) == {'version': 1, 'platform': {}}
    p.write_text(json.dumps({'version': 9, 'platform': {}}), encoding='utf-8')
    assert store.get_platform('tiktok', p) == {}
    store.save_platform('shopee', {'room_id': 'a'}, p)
    assert store.get_platform('shopee', p)['room_id'] == 'a'


def test_atomic_write(tmp_path, monkeypatch):
    p = tmp_path / 'a.json'
    store.save_platform('tiktok', {'room_id': '1'}, p)
    calls = []
    real = os.replace
    monkeypatch.setattr(os, 'replace', lambda a, b: (calls.append((str(a), str(b))), real(a, b))[1])
    store.save_platform('tiktok', {'room_id': '2'}, p)
    assert calls and calls[0][0].endswith('.tmp') and calls[0][1] == str(p)
    assert not list(tmp_path.glob('*.tmp'))
    assert store.get_platform('tiktok', p)['room_id'] == '2'
