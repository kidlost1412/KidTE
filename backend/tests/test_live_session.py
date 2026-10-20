from live_session import VideoLock
import pytest


def locked():
    now = [100.0]
    lock = VideoLock(lambda: now[0])
    ref = {'video_id': 'v', 'duration_s': 600}
    assert not lock.observe(ref, 590, .1)
    now[0] += 8
    assert lock.observe(ref, 598, .1)
    return now, lock, ref


def test_monotonic_loop_eight_hours_and_sparse_verification():
    now, lock, ref = locked()
    now[0] += 8 * 3600
    assert lock.position() == 598
    assert lock.snapshot()['verification_age_s'] == 28800
    assert lock.next_check == 108 + 1800  # nghe kiểm lại 30 phút/lần
    assert lock.reference is ref


def test_two_non_overlapping_excerpts_and_correct_drift_revision():
    now, lock, ref = locked()
    now[0] += 300
    assert not lock.observe(ref, 302, .1)
    now[0] += 8
    assert lock.observe(ref, 310, .1)
    assert lock.position() == 310
    assert lock.revision == 2
    assert lock.next_check == now[0] + 1800


def test_wrong_video_does_not_replace_identity():
    now, lock, ref = locked()
    assert not lock.observe({'video_id': 'other', 'duration_s': 600}, 12, .1)
    assert lock.reference is ref
    assert lock.snapshot()['available'] is False
    assert lock.state == 'uncertain'
    assert lock.next_check == now[0] + 30


def test_overlapping_poll_preserves_first_excerpt():
    now = [100.0]
    lock = VideoLock(lambda: now[0])
    ref = {'video_id': 'v', 'duration_s': 600}
    assert not lock.observe(ref, 20, .1)
    now[0] += 3
    assert not lock.observe(ref, 23, .1)
    now[0] += 6
    assert lock.observe(ref, 29, .1)


def test_reidentify_request_persists_without_changing_source(tmp_path):
    from live_sources import LiveSources
    sources = LiveSources(tmp_path / 'sources.json')
    before = sources.read()['tiktok']
    response = sources.request_reidentify('tiktok')
    after = sources.read()['tiktok']
    assert str(after.pop('reidentify_token')) == response['request_id']
    assert after == before
    with pytest.raises(ValueError):
        sources.request_reidentify('invalid')


@pytest.mark.parametrize('field,value', [('duration_s', float('nan')),
    ('duration_s', 0), ('position', float('inf')), ('margin', False)])
def test_invalid_numbers_never_lock(field, value):
    lock = VideoLock()
    ref = {'video_id': 'v', 'duration_s': 600}
    args = {'position': 10, 'margin': .1}
    (ref if field == 'duration_s' else args)[field] = value
    assert not lock.observe(ref, **args)
    assert lock.reference is None


def test_reverify_interval_default_30_min_clamped_20_to_30():
    from live_session import reverify_seconds
    assert reverify_seconds(None) == 1800
    assert reverify_seconds(20) == 1200
    assert reverify_seconds(25) == 1500
    assert reverify_seconds(5) == 1200      # không nghe dày hơn 20 phút
    assert reverify_seconds(90) == 1800
    assert reverify_seconds("x") == 1800
    now = [0.0]
    lock = VideoLock(lambda: now[0], reverify_s=reverify_seconds(20))
    ref = {'video_id': 'v', 'duration_s': 600}
    lock.observe(ref, 10, .1)
    now[0] += 8
    assert lock.observe(ref, 18, .1)
    assert lock.next_check == 8 + 1200
    # Trong khoảng giữa hai lần kiểm: vẫn khoá, vị trí chạy theo đồng hồ (ghim theo timeline).
    now[0] += 1000
    snap = lock.snapshot()
    assert snap['available'] is True and snap['position_s'] == (18 + 1000) % 600


def test_source_setting_reverify_minutes(tmp_path):
    from live_sources import LiveSources
    store = LiveSources(tmp_path / "s.json")
    assert store.read()["tiktok"]["reverify_minutes"] == 30
    assert store.save({"platform": "tiktok", "settings": {"reverify_minutes": 20}})["tiktok"]["reverify_minutes"] == 20
    for bad in (10, 31, True, "25"):
        with pytest.raises(ValueError):
            store.save({"platform": "tiktok", "settings": {"reverify_minutes": bad}})


def test_reidentify_needs_platform_and_readiness_sends_tiktok(tmp_path):
    from live_sources import LiveSources
    store = LiveSources(tmp_path / "s.json")
    assert store.request_reidentify("tiktok")["ok"] is True
    assert store.read()["tiktok"]["reidentify_token"]
    with pytest.raises(ValueError):
        store.request_reidentify(None)
    import inspect, live_readiness
    assert '_post("/api/live/reidentify", {"platform": "tiktok"}' in inspect.getsource(live_readiness)
