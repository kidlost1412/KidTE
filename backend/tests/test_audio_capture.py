import sys
import threading
from collections import deque
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import audio_capture


DEVICES = [
    {"id": "0", "name": "Microphone", "loopback": False},
    {"id": "1", "name": "VX2728-QHD", "loopback": True},
    {"id": "2", "name": "Speaker", "loopback": True},
    {"id": "3", "name": "Headset", "loopback": False},
    {"id": "4", "name": "HDMI (ezcap GAMEDOCK ULTRA)", "loopback": True},
    {"id": "5", "name": "Line In", "loopback": False},
    {"id": "6", "name": "Digital Output", "loopback": True},
]


def test_list_devices_initializes_com_on_the_calling_thread(monkeypatch):
    events = []

    class Microphone:
        id = "loopback-id"
        name = "Speakers"
        isloopback = True
        channels = 2

    fake_soundcard = mock.Mock()
    fake_soundcard.all_microphones.return_value = [Microphone()]
    token = object()
    monkeypatch.setattr(audio_capture, "sc", fake_soundcard)
    monkeypatch.setattr(audio_capture, "_initialize_audio_thread",
                        lambda: events.append("com_init") or token)
    monkeypatch.setattr(audio_capture, "_uninitialize_audio_thread",
                        lambda value: events.append(("com_uninit", value)))

    assert audio_capture.list_devices() == [{
        "id": "loopback-id", "name": "Speakers",
        "loopback": True, "channels": 2,
    }]
    assert events == ["com_init", ("com_uninit", token)]


def test_forbidden_dock_cannot_be_selected_by_id_index_or_name():
    with mock.patch.object(audio_capture, "list_devices", return_value=DEVICES):
        for selector in ("4", "ezcap", "4"):
            with pytest.raises(RuntimeError, match="cấm"):
                audio_capture.resolve_device(selector)


def test_probe_never_opens_forbidden_dock_from_real_seven_device_shape(monkeypatch):
    opened = []

    class Capture:
        def __init__(self, selector, *_args, **_kwargs):
            opened.append(selector); self.error = ""; self.device = selector
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def latest(self): return None

    monkeypatch.setattr(audio_capture, "list_devices", lambda: DEVICES)
    monkeypatch.setattr(audio_capture, "RollingCapture", Capture)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    audio_capture.probe_all(seconds=0)
    assert "4" not in opened
    assert opened == ["1", "2", "6"]


def test_capture_thread_initializes_com_before_opening_wasapi(monkeypatch):
    events = []
    capture = audio_capture.RollingCapture.__new__(audio_capture.RollingCapture)
    capture.rate = 4
    capture._buffer = deque(maxlen=8)
    capture._lock = threading.Lock()
    capture._stop = threading.Event()
    capture._error = None

    class Recorder:
        def __enter__(self):
            events.append("recorder_enter")
            return self
        def __exit__(self, *_args):
            events.append("recorder_exit")
        def record(self, numframes):
            capture._stop.set()
            return np.zeros((numframes, 1), dtype=np.float32)

    class Microphone:
        def recorder(self, **_kwargs):
            return Recorder()

    capture._microphone = Microphone()
    token = object()
    monkeypatch.setattr(audio_capture, "_initialize_audio_thread",
                        lambda: events.append("com_init") or token)
    monkeypatch.setattr(audio_capture, "_uninitialize_audio_thread",
                        lambda value: events.append(("com_uninit", value)))

    capture._pump()

    assert capture.error is None
    assert events == ["com_init", "recorder_enter", "recorder_exit",
                      ("com_uninit", token)]


def test_capture_constructor_keeps_com_while_getting_microphone(monkeypatch):
    events = []
    token = object()
    monkeypatch.setattr(audio_capture, "_initialize_audio_thread",
                        lambda: events.append("com_init") or token)
    monkeypatch.setattr(audio_capture, "_uninitialize_audio_thread",
                        lambda value: events.append(("com_uninit", value)))
    monkeypatch.setattr(audio_capture, "resolve_device", lambda _selector: {
        "id": "speaker-id", "name": "Speakers", "loopback": True,
    })
    monkeypatch.setattr(audio_capture.sc, "get_microphone",
                        lambda *_args, **_kwargs: events.append("get_microphone") or object())

    audio_capture.RollingCapture("speaker-id", 1.0)

    assert events == ["com_init", "get_microphone", ("com_uninit", token)]
