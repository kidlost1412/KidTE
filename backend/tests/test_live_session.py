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
    assert lock.next_check == 408
    assert lock.reference is ref


def test_two_non_overlapping_excerpts_and_correct_drift_revision():
    now, lock, ref = locked()
    now[0] += 300
    assert not lock.observe(ref, 302, .1)
    now[0] += 8
    assert lock.observe(ref, 310, .1)
    assert lock.position() == 310
    assert lock.revision == 2
    assert lock.next_check == now[0] + 300


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
