import importlib.util
import time
from pathlib import Path

import live_state_store as store


def _module(monkeypatch, tmp_path):
    folder = Path(__file__).resolve().parents[2] / 'plugins' / 'dong-bo-video'
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location('isolated_live_sync_shopee', folder / 'live_sync.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = tmp_path / 'live_state.json'
    monkeypatch.setattr(store, 'default_path', lambda: state)
    return mod, state


class FakeClient:
    def __init__(self, path, cursor=5000):
        self.path, self.cursor = path, cursor

    def media_inputs(self):
        return [{'input_name': 'vid', 'media_path': str(self.path), 'local_file': True,
                 'mediaCursor': self.cursor, 'mediaState': 'OBS_MEDIA_STATE_PLAYING'}]

    def request(self, *a, **k):
        return {'mediaCursor': self.cursor}

    def close(self):
        pass


def _setup(mod, monkeypatch, calls, result='v1'):
    monkeypatch.setattr(mod, 'load_references', lambda: [])
    def verify(path, refs):
        calls.append(path)
        return result
    monkeypatch.setattr(mod, 'verify_obs_file', verify)


def test_restart_reuses_binding_and_uses_live_cursor(monkeypatch, tmp_path):
    mod, state = _module(monkeypatch, tmp_path)
    f = tmp_path / 'a.mp4'
    f.write_bytes(b'x')
    calls = []
    _setup(mod, monkeypatch, calls)
    first = mod.VerifiedObs(FakeClient(f, 1000))
    first.room_id = 'room9'
    assert first.media_inputs()[0]['verified_video_id'] == 'v1'
    assert len(calls) == 1
    saved = store.get_platform('shopee')
    assert saved['room_id'] == 'room9' and saved['obs_binding']['last_cursor'] == 1000
    second = mod.VerifiedObs(FakeClient(f, 77000))
    second.persisted = store.get_platform('shopee')
    item = second.media_inputs()[0]
    assert item['verified_video_id'] == 'v1' and item['mediaCursor'] == 77000
    assert len(calls) == 1


def test_identity_change_age_and_input_force_verification(monkeypatch, tmp_path):
    mod, state = _module(monkeypatch, tmp_path)
    f = tmp_path / 'a.mp4'
    f.write_bytes(b'x')
    calls = []
    _setup(mod, monkeypatch, calls)
    mod.VerifiedObs(FakeClient(f)).media_inputs()
    entry = store.get_platform('shopee')
    f.write_bytes(b'xyz')  # size/mtime change
    v = mod.VerifiedObs(FakeClient(f)); v.persisted = entry; v.media_inputs()
    assert len(calls) == 2
    old = {'obs_binding': {**entry['obs_binding'], 'measured_at': time.time() - 700}}
    assert store.fresh_obs_binding(old, 'vid', entry['obs_binding']['identity']) is None
    assert store.fresh_obs_binding(entry, 'other', entry['obs_binding']['identity']) is None
    assert store.fresh_obs_binding(entry, 'vid', ['p', 1, 2, 0]) is None


def test_corrupt_file_and_write_failure_ignored(monkeypatch, tmp_path):
    mod, state = _module(monkeypatch, tmp_path)
    f = tmp_path / 'a.mp4'
    f.write_bytes(b'x')
    state.write_text('{not json')
    assert store.get_platform('shopee') == {}
    calls = []
    _setup(mod, monkeypatch, calls)
    monkeypatch.setattr(store, 'save_platform', lambda *a, **k: (_ for _ in ()).throw(OSError('disk')))
    v = mod.VerifiedObs(FakeClient(f))
    assert v.media_inputs()[0]['verified_video_id'] == 'v1'


def test_periodic_refresh_keeps_binding_fresh_and_stop_expires(monkeypatch, tmp_path):
    mod, state = _module(monkeypatch, tmp_path)
    f = tmp_path / 'a.mp4'
    f.write_bytes(b'x')
    calls = []
    _setup(mod, monkeypatch, calls)
    clock = [1_000_000.0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    writes = []
    real_save = store.save_platform
    monkeypatch.setattr(store, 'save_platform', lambda *a, **k: (writes.append(1), real_save(*a, **k))[1])
    v = mod.VerifiedObs(FakeClient(f))
    v.media_inputs()
    for _ in range(900):  # 30 min of 2 s loops
        clock[0] += 2
        v.media_inputs()
    assert len(calls) == 1 and len(writes) <= 32
    clock[0] += 60
    r = mod.VerifiedObs(FakeClient(f, 99))
    r.persisted = store.get_platform('shopee')
    assert r.media_inputs()[0]['verified_video_id'] == 'v1' and len(calls) == 1
    # media stops: no refresh; >10 min later restart must verify again
    v.client.media_inputs = lambda: [{**FakeClient(f).media_inputs()[0], 'mediaState': 'OBS_MEDIA_STATE_STOPPED'}]
    for _ in range(400):
        clock[0] += 2
        v.media_inputs()
    r2 = mod.VerifiedObs(FakeClient(f))
    r2.persisted = store.get_platform('shopee')
    r2.media_inputs()
    assert len(calls) == 2
