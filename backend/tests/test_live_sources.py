from pathlib import Path

import pytest
from live_sources import LiveSources
from obs_source import ObsClient, obs_measurement, resolve_obs_video
from server import ReplyEngine, VideoPositionStore


def test_two_platforms_keep_different_videos_and_independent_loss():
    engine = ReplyEngine.__new__(ReplyEngine)
    engine.video_positions = {name: VideoPositionStore() for name in ("tiktok", "shopee")}
    engine.set_video_position({"platform":"shopee", "source":"obs_media", "video_id":"a", "position_s":10})
    engine.set_video_position({"platform":"tiktok", "source":"tiktok_process_audio", "video_id":"b", "position_s":90})
    assert engine.current_video_position("shopee")["video_id"] == "a"
    assert engine.current_video_position("tiktok")["video_id"] == "b"
    engine.set_video_position({"platform":"shopee", "available":False})
    assert not engine.current_video_position("shopee")["available"]
    assert engine.current_video_position("tiktok")["available"]
    with pytest.raises(ValueError):
        engine.set_video_position({"platform":"shopee", "source":"tiktok_process_audio", "position_s":1})
    with pytest.raises(ValueError):
        engine.current_video_position("other")


def test_settings_are_private_validated_and_durable(tmp_path):
    store = LiveSources(tmp_path / "settings.json")
    store.save({"platform":"shopee", "settings":{"password":"private-test", "offset_ms":-2500}})
    store.save({"platform":"tiktok", "settings":{"offset_ms":500}})
    assert store.read()["shopee"]["password"] == "private-test"
    assert "password" not in store.read(public=True)["shopee"]
    assert store.read(public=True)["shopee"]["password_configured"]
    assert LiveSources(store.path).read()["tiktok"]["offset_ms"] == 500
    with pytest.raises(ValueError):
        store.save({"platform":"shopee", "settings":{"mode":"device"}})
    with pytest.raises(ValueError):
        store.save({"platform":"tiktok", "settings":{"offset_ms":float("nan")}})
    with pytest.raises(ValueError, match="offset_out_of_range"):
        store.save({"platform":"tiktok", "settings":{"offset_ms":10**400}})
    with pytest.raises(ValueError):
        store.save({"platform":"other", "settings":{}})


def test_uncertain_position_keeps_video_but_cannot_drive_pin(monkeypatch):
    import server
    now = [100.0]
    monkeypatch.setattr(server.time, 'monotonic', lambda: now[0])
    engine = ReplyEngine.__new__(ReplyEngine)
    engine.video_positions = {'tiktok': VideoPositionStore()}
    engine.set_video_position({'platform': 'tiktok', 'available': False,
        'state': 'uncertain', 'video_id': 'v', 'position_s': 20,
        'video_duration_s': 600, 'lock_id': 'lock', 'verification_age_s': 300})
    now[0] += 3
    result = engine.current_video_position('tiktok')
    assert result['video_id'] == 'v'
    assert result['available'] is False
    assert result['lock_id'] == 'lock'
    assert result['heartbeat_age_s'] == 3
    assert result['verification_age_s'] == 303


def item(path="D:/videos/a.mp4", cursor=25000):
    return {"input_name":"Media", "media_path":path, "local_file":True,
        "verified_video_id":"a" if path.endswith("a.mp4") else None,
        "mediaCursor":cursor, "mediaDuration":120000, "mediaState":"OBS_MEDIA_STATE_PLAYING"}


class FakeObs:
    def __init__(self, items=None, live=True):
        self.items = [item()] if items is None else items
        self.live = live
    def media_inputs(self):
        return self.items
    def request(self, kind):
        assert kind == "GetStreamStatus"
        return {"outputActive":self.live}


def test_obs_uses_actual_cursor_and_own_offset():
    measured, reason, _ = obs_measurement(FakeObs(), {"offset_ms":-2000},
        {"a":{"source_file":"a.mp4", "cycle_ms":120000}})
    assert reason == ""
    assert measured["position_s"] == 23
    assert measured["source"] == "obs_media"


def test_obs_negative_offset_wraps_to_end_of_video():
    measured, reason, _ = obs_measurement(FakeObs(items=[item(cursor=1000)]),
        {'offset_ms': -2000}, {'a': {'cycle_ms': 120000}})
    assert reason == ''
    assert measured['position_s'] == 119


@pytest.mark.parametrize('field,value', [('mediaDuration', float('nan')),
    ('mediaCursor', float('inf')), ('mediaCursor', 'invalid')])
def test_obs_invalid_measurement_never_reaches_pin(field, value):
    measured, reason, _ = obs_measurement(FakeObs(items=[{**item(), field: value}]),
        {}, {'a': {'cycle_ms': 120000}})
    assert measured is None
    assert reason == 'obs_position_invalid'


@pytest.mark.parametrize("case,reason", [
    (FakeObs(live=False),"obs_not_streaming"),
    (FakeObs(items=[]),"obs_media_not_playing"),
    (FakeObs(items=[item(),{**item(),"input_name":"Other"}]),"obs_select_one_media"),
    (FakeObs(items=[{**item(),"mediaState":"OBS_MEDIA_STATE_PAUSED"}]),"obs_media_not_playing"),
    (FakeObs(items=[item("D:/unknown.mp4")]),"obs_video_not_linked"),
    (FakeObs(items=[{**item(),"mediaDuration":5000}]),"obs_video_duration_mismatch"),
])
def test_obs_never_invents_position_for_missing_or_ambiguous_source(case, reason):
    measured, actual, _ = obs_measurement(case, {}, {"a":{"source_file":"a.mp4","cycle_ms":120000}})
    assert measured is None
    assert actual == reason


def test_obs_binding_tracks_file_not_reused_input_name():
    settings = {"bindings":{"D:/videos/a.mp4":"a"}}
    assert resolve_obs_video(item(), settings, {"a":{}}) == "a"
    assert resolve_obs_video(item("D:/videos/changed.mp4"),settings,{"a":{}}) is None
    assert resolve_obs_video({**item(),"verified_video_id":None}, {}, {"a":{"source_file":"a.mp4"}}) is None
    client = ObsClient.__new__(ObsClient)
    with pytest.raises(ValueError, match="obs_read_only"):
        client.request("SetCurrentProgramScene")


def test_recognition_confirmation_never_links_different_videos_and_handles_loop():
    folder = Path(__file__).resolve().parents[2] / "plugins" / "dong-bo-video"
    import sys
    sys.path.insert(0, str(folder))
    from live_sync import consistent
    assert not consistent(("a",10,100),"b",13,103,120)
    assert consistent(("a",118,100),"a",1,103,120)
    assert not consistent(("a",10,100),"a",60,103,120)
