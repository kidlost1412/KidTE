"""Regression: Shopee video recognition died under split packages (video-sync 1.0.0).

The frozen KidTEVideo.exe 1.0.0 bundles an old component_store that rejects the
split-generation active.json, so find_tool('ffmpeg') raised ComponentStoreError, the OBS
loop dropped its connection every 2 s and the health file (also NUL-corrupted after a
power cut) never told the operator why.
"""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_state_store as state_store
import plugin_runtime
from obs_source import obs_measurement
from service_supervisor import ServiceSpec, ServiceSupervisor

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "dong-bo-video"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def live_sync(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(PLUGIN))
    module = _load("shopee_split_live_sync", "live_sync.py")
    monkeypatch.setattr(state_store, "default_path", lambda: tmp_path / "live_state.json")
    return module


class Client:
    def __init__(self, path):
        self.path = path

    def media_inputs(self):
        return [{"input_name": "vid", "media_path": str(self.path), "local_file": True,
                 "mediaCursor": 5000, "mediaDuration": 60000, "mediaState": "OBS_MEDIA_STATE_PLAYING"}]

    def request(self, kind, **_):
        if kind == "GetStreamStatus":
            return {"outputActive": True}
        return {"mediaCursor": 5000}

    def close(self):
        pass


def test_ffmpeg_failure_keeps_connection_and_reports_reason(live_sync, monkeypatch, tmp_path):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    monkeypatch.setattr(live_sync, "load_references", lambda: [])
    calls = []

    def broken(path, refs):
        calls.append(path)
        raise RuntimeError("invalid component mapping")
    monkeypatch.setattr(live_sync, "verify_obs_file", broken)
    obs = live_sync.VerifiedObs(Client(media))
    items = obs.media_inputs()          # must not raise
    assert items[0]["verified_video_id"] is None and "invalid component mapping" in items[0]["verify_error"]
    assert obs_measurement(obs, {"input_name": ""}, {"v": {"cycle_ms": 60000}})[1] == "obs_verify_failed"
    assert len(calls) == 1              # cached: not retried every 2 s


def test_ffmpeg_recovery_after_failure(live_sync, monkeypatch, tmp_path):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    monkeypatch.setattr(live_sync, "load_references", lambda: [])
    outcomes = [RuntimeError("missing_ffmpeg"), "v1"]

    def verify(path, refs):
        value = outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(live_sync, "verify_obs_file", verify)
    obs = live_sync.VerifiedObs(Client(media))
    assert "verify_error" in obs.media_inputs()[0]
    key = next(iter(obs.cache))
    obs.cache[key] = (None, obs.cache[key][1] - 31)    # retry window elapsed
    item = obs.media_inputs()[0]
    assert item["verified_video_id"] == "v1" and "verify_error" not in item


def test_write_health_recovers_from_nul_filled_file(tmp_path):
    video = _load("shopee_split_video_sync", "video_sync.py")
    path = tmp_path / "live-shopee.health.json"
    path.write_bytes(b"\0" * 527)       # NTFS zero-fill after an unclean shutdown
    saved = video.write_health(path, state="waiting", reason="obs_media_not_playing")
    assert saved and saved["reason"] == "obs_media_not_playing"
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "waiting"


def test_service_spec_env_overrides_and_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("KIDTE_APP_ROOT", "D:/App")
    captured = {}

    class Proc:
        pid = 4242

        def poll(self):
            return None
    supervisor = ServiceSupervisor(tmp_path, popen=lambda cmd, **kw: captured.update(kw) or Proc(),
                                   pid_alive=lambda _p: False)
    spec = ServiceSpec("dich-vu-mau", ("x.exe",), tmp_path,
                       (("KIDTE_MEDIA_TOOLS_DIR", "T"), ("KIDTE_APP_ROOT", None)))
    supervisor.start(spec)
    assert captured["env"]["KIDTE_MEDIA_TOOLS_DIR"] == "T" and "KIDTE_APP_ROOT" not in captured["env"]
    plain = {}
    ServiceSupervisor(tmp_path / "b", popen=lambda cmd, **kw: plain.update(kw) or Proc(),
                      pid_alive=lambda _p: False).start(ServiceSpec("dich-vu-mau", ("x.exe",), tmp_path))
    assert "env" not in plain           # untouched behaviour for other services


def test_video_service_gets_verified_media_tools_and_no_split_active_json(monkeypatch, tmp_path):
    tools = tmp_path / "packages" / "media-tools" / "1.0.0" / "tools"
    tools.mkdir(parents=True)
    exe = tmp_path / "KidTEVideo.exe"
    exe.write_bytes(b"x")

    def component_path(component, rel):
        return {"media-tools": tools / Path(rel).name, "video-sync": exe}[component]
    monkeypatch.setitem(sys.modules, "component_runtime", SimpleNamespace(component_path=component_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    manifest = SimpleNamespace(plugin_id="dong-bo-video", kind="dich-vu", command=("KidTEVideo.exe",),
                               directory=tmp_path, command_path=exe)
    spec = plugin_runtime.PluginRuntimeManager._spec(manifest)
    env = dict(spec.env)
    assert env["KIDTE_MEDIA_TOOLS_DIR"] == str(tools)
    assert env["PATH"].startswith(str(tools))
    assert env["KIDTE_APP_ROOT"] is None and env["KID_TE_ROOT"]


def test_find_tool_prefers_core_verified_media_tools_dir(monkeypatch, tmp_path):
    import config
    (tmp_path / "ffmpeg.exe").write_bytes(b"x")
    (tmp_path / "ffmpeg").write_bytes(b"x")
    monkeypatch.setenv("KIDTE_MEDIA_TOOLS_DIR", str(tmp_path))
    assert Path(config.find_tool("ffmpeg")).parent == tmp_path
