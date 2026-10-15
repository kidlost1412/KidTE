"""Video LIVE rat lon (toi 100 GiB): kiem cho trong o va van tay WAV theo khoi."""
import io
import wave
from collections import namedtuple

import numpy as np
import pytest
import video_analysis
import video_fingerprint as vf

Usage = namedtuple("usage", "total used free")
GiB = 1024**3


def _manager(tmp_path, monkeypatch, free):
    monkeypatch.setattr(video_analysis, "_require_analysis_runtime", lambda: None)
    monkeypatch.setattr(video_analysis.shutil, "disk_usage", lambda _p: Usage(0, 0, free))
    return video_analysis.VideoAnalysisManager(root=tmp_path, catalog_provider=lambda: {
        "products": [{"product_id": "p", "title": "Product"}]})


def test_start_rejects_when_disk_too_small_and_creates_nothing(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, free=61 * GiB)
    with pytest.raises(video_analysis.InsufficientDiskSpace, match="insufficient_disk_space") as err:
        manager.start_upload("live.mp4", 60 * GiB, "a" * 64)
    assert err.value.needed == 62 * GiB
    assert not list(tmp_path.iterdir())


def test_start_accepts_with_margin(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, free=62 * GiB)
    assert manager.start_upload("live.mp4", 60 * GiB, "a" * 64)["state"] == "uploading"


def test_resume_checks_only_remaining_bytes(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, free=100 * GiB)
    size = 10 * GiB
    job = manager.start_upload("live.mp4", size, "b" * 64)
    manager.append_chunk(job["job_id"], 0, io.BytesIO(b"x" * 1024), 1024)
    remaining = size - 1024
    monkeypatch.setattr(video_analysis.shutil, "disk_usage",
                        lambda _p: Usage(0, 0, remaining + 2 * GiB))
    assert manager.resumable_job("live.mp4", size, "b" * 64)["job_id"] == job["job_id"]
    monkeypatch.setattr(video_analysis.shutil, "disk_usage",
                        lambda _p: Usage(0, 0, remaining + 2 * GiB - 1))
    with pytest.raises(ValueError, match="insufficient_disk_space"):
        manager.resumable_job("live.mp4", size, "b" * 64)


def _write_wav(path, samples, rate, channels):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype("<i2").tobytes())


def _old_fingerprint(path):
    samples, rate = vf.read_pcm16(path)
    samples, rate = vf.resample_mono(samples, rate)
    return vf.spectral_features(samples, rate), len(samples) / rate


@pytest.mark.parametrize("rate,channels,seconds,block", [
    (16000, 1, 37.3, 16000 * 5 + 123),
    (16000, 2, 21.0, 4000),
    (16000, 1, 0.1, 16000),
    (48000, 2, 12.0, 16000),
])
def test_streaming_fingerprint_identical_to_full_read(tmp_path, rate, channels, seconds, block):
    rng = np.random.default_rng(1234 + channels)
    frames = int(rate * seconds)
    data = rng.integers(-32768, 32767, size=frames * channels, dtype=np.int32)
    path = tmp_path / "a.wav"
    _write_wav(path, data, rate, channels)
    expected, expected_duration = _old_fingerprint(path)
    got, duration = vf.spectral_features_wav(path, block_frames=block)
    assert got.dtype == expected.dtype and got.shape == expected.shape
    assert np.array_equal(got, expected)
    assert duration == expected_duration
