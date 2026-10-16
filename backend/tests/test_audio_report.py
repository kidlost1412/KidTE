"""Báo cáo thiết bị âm thanh: phân loại lỗi, thiết bị cũ không còn, rescan, log khi lỗi đổi."""
import logging
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import audio_capture  # noqa: E402
import server as server_module  # noqa: E402

SPEAKER = {"id": "{new-guid}", "name": "Loa Realtek", "loopback": True}
MIC = {"id": "{mic}", "name": "Micro", "loopback": False}


@pytest.fixture
def env(monkeypatch):
    state = {"devices": [SPEAKER, MIC], "services": {"Audiosrv": "running", "AudioEndpointBuilder": "running"},
             "build": 22631, "raise": None}
    fake_sc = types.SimpleNamespace(default_speaker=lambda: types.SimpleNamespace(name="Loa Realtek"))
    monkeypatch.setattr(audio_capture, "sc", fake_sc)

    def devices():
        if state["raise"]:
            raise state["raise"]
        return list(state["devices"])
    monkeypatch.setattr(audio_capture, "list_devices", devices)
    monkeypatch.setattr(audio_capture, "_service_state", lambda name: state["services"][name])
    monkeypatch.setattr(audio_capture, "_windows_build", lambda: state["build"])
    monkeypatch.setattr(audio_capture, "_mic_privacy", lambda: "Allow")
    return state


def test_ok(env):
    report = audio_capture.enumerate_report()
    assert report["error_code"] == "ok"
    assert report["default_speaker_name"] == "Loa Realtek"
    assert report["process_loopback_supported"] is True


def test_soundcard_unavailable(env, monkeypatch):
    monkeypatch.setattr(audio_capture, "sc", None)
    assert audio_capture.enumerate_report()["error_code"] == "soundcard_unavailable"


def test_audio_service_stopped(env):
    env["devices"] = []
    env["services"]["Audiosrv"] = "stopped"
    report = audio_capture.enumerate_report()
    assert report["error_code"] == "audio_service_stopped"
    assert "Audiosrv" in report["error_detail"]


def test_no_active_output(env):
    env["devices"] = [MIC]
    assert audio_capture.enumerate_report()["error_code"] == "no_active_output"


def test_enumeration_failed_carries_hresult(env):
    exc = OSError("boom")
    exc.winerror = -2147024891
    env["raise"] = exc
    report = audio_capture.enumerate_report()
    assert report["error_code"] == "enumeration_failed"
    assert report["error_detail"] == "OSError 0x80070005"


def test_old_build_not_supported(env):
    env["build"] = 18363
    assert audio_capture.enumerate_report()["process_loopback_supported"] is False


def test_unreadable_services_are_ignored(env):
    env["devices"] = []
    env["services"] = {"Audiosrv": None, "AudioEndpointBuilder": None}
    assert audio_capture.enumerate_report()["error_code"] == "no_active_output"


class Sources:
    def __init__(self, tiktok):
        self.tiktok = tiktok

    def read(self, public=True):
        return {"tiktok": self.tiktok}


def make_engine(tiktok, saved="auto"):
    eng = object.__new__(server_module.ReplyEngine)
    eng.live_sources = Sources(tiktok)
    eng.audio_device_id = saved
    return eng


def test_missing_saved_device_suggests_default_without_saving(env):
    tiktok = {"mode": "device", "device_id": "{old-guid}"}
    eng = make_engine(tiktok, saved="{old-audio}")
    report = eng.audio_report()
    sources = {item["source"] for item in report["missing_saved_devices"]}
    assert sources == {"audio_device_id", "live_sources.tiktok"}
    assert report["suggested_device"] == {"id": "{new-guid}", "name": "Loa Realtek"}
    assert tiktok["device_id"] == "{old-guid}" and eng.audio_device_id == "{old-audio}"


def test_valid_device_or_process_mode_has_no_suggestion(env):
    assert make_engine({"mode": "device", "device_id": "{new-guid}"}).audio_report()["suggested_device"] is None
    report = make_engine({"mode": "process", "device_id": ""}).audio_report()
    assert report["suggested_device"] is None and report["missing_saved_devices"] == []


def test_rescan_returns_fresh_report(env):
    eng = make_engine({"mode": "process"})
    assert eng.audio_report()["error_code"] == "ok"
    env["devices"] = []
    assert eng.audio_report()["error_code"] == "no_active_output"


def test_logs_only_when_error_changes(env, caplog):
    eng = make_engine({"mode": "process"})
    env["devices"] = [MIC]
    with caplog.at_level(logging.WARNING, logger="kid-te.audio"):
        eng.audio_report()
        eng.audio_report()
        env["devices"] = [SPEAKER]
        eng.audio_report()
        env["devices"] = []
        env["services"]["Audiosrv"] = "stopped"
        eng.audio_report()
    events = [r.getMessage() for r in caplog.records if "audio_enumeration_failed" in r.getMessage()]
    assert len(events) == 2
    assert "no_active_output" in events[0] and "audio_service_stopped" in events[1]
    assert "Loa Realtek" not in "".join(events)
