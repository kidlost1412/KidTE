import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from server import ReplyEngine


def test_same_name_download_does_not_override_verified_original(tmp_path, monkeypatch):
    expected = b'verified original fixture'
    digest = hashlib.sha256(expected).hexdigest()
    downloads = tmp_path / 'Downloads'
    downloads.mkdir()
    (downloads / 'same.mp4').write_bytes(b'different video same filename')
    jobs = tmp_path / 'jobs'
    job = jobs / 'one'
    job.mkdir(parents=True)
    (job / 'job.json').write_text(json.dumps({'video_id':'v','filename':'same.mp4','source_sha256':digest}))
    correct = job / 'same.mp4'
    correct.write_bytes(expected)
    engine = ReplyEngine.__new__(ReplyEngine)
    engine._lock = threading.Lock()
    engine.workspace = SimpleNamespace(detail=lambda *args: {'data':{'timeline':{
        'source_file':'same.mp4', 'source_sha256':digest}}})
    engine.video_analysis = SimpleNamespace(root=jobs)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert engine.video_media_path('v') == correct
    correct.write_bytes(b'corrupted original')
    with pytest.raises(ValueError, match='not_available'):
        engine.video_media_path('v')


def test_media_hash_cache_is_reused_but_invalidated_by_file_change(tmp_path, monkeypatch):
    path = tmp_path / 'video.mp4'
    path.write_bytes(b'fixture')
    digest = hashlib.sha256(b'fixture').hexdigest()
    engine = ReplyEngine.__new__(ReplyEngine)
    engine._lock = threading.Lock()
    opened = []
    original = Path.open
    def tracked(self, *args, **kwargs):
        if self == path: opened.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', tracked)
    for _ in range(30):
        assert engine._media_source_matches(path, digest)
    assert len(opened) == 1
    path.write_bytes(b'new different fixture')
    assert not engine._media_source_matches(path, digest)
    assert engine._media_source_matches(path, hashlib.sha256(b'new different fixture').hexdigest())
