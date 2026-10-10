"""Exercise the actual tracker loop with fake audio, clock and HTTP."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
from live_session import VideoLock


class StopLoop(BaseException):
    pass


def test_locked_tracker_releases_audio_and_only_verifies_locked_reference(monkeypatch):
    folder = Path(__file__).resolve().parents[2] / 'plugins' / 'dong-bo-video'
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location('isolated_live_sync', folder / 'live_sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = [100.0]
    active = [0]
    captures = []
    searches = []
    published = []
    references = [{'video_id': 'v', 'duration_s': 600}, {'video_id': 'other', 'duration_s': 600}]

    class Capture:
        error = None
        name = 'fake'
        rate = 8000
        def __init__(self, *args):
            captures.append(self)
            self.started = None
        def __enter__(self):
            active[0] += 1
            self.started = now[0]
        def close(self):
            active[0] -= 1
        def latest(self):
            return np.ones(64000) if now[0] - self.started >= 8 else None

    def sleep(seconds):
        now[0] += seconds
        if now[0] > 450:
            raise StopLoop()
        # During the five minute locked period no capture remains running.
        if 125 < now[0] < 400:
            assert active[0] == 0

    def match(rows, block, rate):
        searches.append([row['video_id'] for row in rows])
        return rows[0], (now[0] - 8) % 600, .9, .1

    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.time, 'sleep', sleep)
    monkeypatch.setattr(module, 'VideoLock', lambda: VideoLock(lambda: now[0]))
    monkeypatch.setattr(module, 'ProcessCapture', Capture)
    monkeypatch.setattr(module, 'studio_process', lambda name: {'pid': 1, 'create_time': 1})
    monkeypatch.setattr(module, 'load_references', lambda: references)
    monkeypatch.setattr(module, 'best_match', match)
    monkeypatch.setattr(module, 'health', lambda *args, **kwargs: None)
    monkeypatch.setattr(module, 'publish', lambda platform, measurement=None: published.append(measurement))
    settings = {'enabled': True, 'mode': 'process', 'device_id': '',
                'process_name': 'fake', 'offset_ms': 0}
    with pytest.raises(StopLoop):
        module.tiktok_loop(SimpleNamespace(read=lambda: {'tiktok': settings}))
    assert len(captures) == 2
    first_locked = next(i for i, row in enumerate(published) if row and row.get('lock_id'))
    assert all(row['video_id'] == 'v' for row in published[first_locked:] if row)
    full_searches = [rows for rows in searches if len(rows) == 2]
    assert len(full_searches) == 4
    assert searches[-1] == ['v']
