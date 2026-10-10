from live_session import VideoLock


def locked():
    now = [100.0]
    lock = VideoLock(lambda: now[0])
    ref = {'video_id': 'v', 'duration_s': 600}
    assert not lock.observe(ref, 590, .1)
    now[0] += 8
    assert lock.observe(ref, 598, .1)
    return now, lock, ref


def test_uncertain_lock_switches_to_new_video_after_two_consistent_excerpts():
    now, lock, ref = locked()
    other = {'video_id': 'other', 'duration_s': 900}
    assert not lock.observe(other, 12, .1)  # đang khoá: chỉ chuyển sang chưa chắc chắn
    assert lock.state == 'uncertain' and lock.reference is ref
    old_lock, old_revision = lock.lock_id, lock.revision
    now[0] += 30
    assert not lock.observe(other, 50, .1)
    assert lock.reference is ref
    now[0] += 8
    assert lock.observe(other, 58, .1)
    assert lock.reference is other and lock.state == 'locked'
    assert lock.lock_id != old_lock and lock.revision == old_revision + 1
    assert lock.position() == 58


def test_uncertain_lock_does_not_switch_on_inconsistent_excerpts():
    now, lock, ref = locked()
    other = {'video_id': 'other', 'duration_s': 900}
    lock.observe(other, 12, .1)
    now[0] += 30
    assert not lock.observe(other, 50, .1)
    now[0] += 8
    assert not lock.observe(other, 300, .1)
    assert lock.reference is ref and lock.state == 'uncertain'


def test_uncertain_lock_revalidates_same_video():
    now, lock, ref = locked()
    lock.uncertain()
    now[0] += 30
    assert not lock.observe(ref, (598 + 30) % 600, .1)
    now[0] += 8
    assert lock.observe(ref, (598 + 38) % 600, .1)
    assert lock.reference is ref and lock.state == 'locked'
