"""Video identity lock and monotonic position, independent of audio devices."""
from __future__ import annotations

import math
import time
import uuid

# Đã khoá video thì KHÔNG nghe lại liên tục: chỉ nghe kiểm định kỳ thưa (mặc định 30 phút,
# cho phép 20–30 phút qua cài đặt TikTok "reverify_minutes"). Đổi video giữa chừng -> nút "Nghe lại".
REVERIFY_DEFAULT_S = 30 * 60
REVERIFY_MIN_S = 20 * 60
REVERIFY_MAX_S = 30 * 60


def reverify_seconds(minutes=None):
    try:
        value = float(minutes) * 60
    except (TypeError, ValueError):
        return REVERIFY_DEFAULT_S
    if not math.isfinite(value):
        return REVERIFY_DEFAULT_S
    return min(REVERIFY_MAX_S, max(REVERIFY_MIN_S, value))


class VideoLock:
    def __init__(self, clock=time.monotonic, reverify_s=REVERIFY_DEFAULT_S):
        self.clock = clock
        self.reverify_s = reverify_s
        self.reset()

    def reset(self):
        self.state = 'unidentified'
        self.reference = None
        self.lock_id = ''
        self.revision = 0
        self.position_s = 0.0
        self.anchor = self.clock()
        self.verified_at = None
        self.next_check = self.anchor
        self.previous = None

    def position(self, now=None):
        now = self.clock() if now is None else now
        if self.reference is None:
            return None
        return (self.position_s + now - self.anchor) % self.reference['duration_s']

    def uncertain(self):
        if self.reference is not None:
            self.state = 'uncertain'
            self.previous = None
            self.next_check = self.clock() + 30

    def observe(self, reference, position, margin):
        """Two independent consistent excerpts are required to lock/revalidate."""
        now = self.clock()
        duration = reference.get('duration_s')
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                not math.isfinite(v) for v in (position, margin, duration)) or
                duration <= 0 or margin < .06):
            self.previous = None
            return False
        switching = self.reference is not None and reference['video_id'] != self.reference['video_id']
        if switching and self.state != 'uncertain':
            self.uncertain()
            return False
        # Đang chưa chắc chắn: hai đoạn khớp nhất quán của video KHÁC thì khoá sang video đó.
        position %= duration
        previous = self.previous
        if previous is not None and now - previous[2] < 8:
            return False
        self.previous = (reference['video_id'], position, now)
        if self.reference is None:
            self.state = 'locking'
        if previous is None or previous[0] != reference['video_id'] or now - previous[2] < 8:
            return False
        delta = (position - previous[1] - (now - previous[2])) % duration
        if min(delta, duration - delta) >= 2.5:
            return False
        predicted = None if switching else self.position(now)
        drift = 0 if predicted is None else (position - predicted) % duration
        drift = min(drift, duration - drift)
        if self.reference is None or switching:
            self.reference = reference
            self.lock_id = uuid.uuid4().hex
            self.revision += 1
            self.position_s, self.anchor = position, now
        elif drift > 1:
            self.position_s, self.anchor = position, now
            self.revision += 1
        self.state = 'locked'
        self.verified_at = now
        self.next_check = now + self.reverify_s
        self.previous = None
        return True

    def export(self, library_revision=''):
        """Serializable hint for restart recovery (video identity + last position)."""
        if self.reference is None:
            return None
        return {'video_id': self.reference['video_id'],
                'duration_s': self.reference['duration_s'],
                'position_s': self.position(), 'lock_id': self.lock_id,
                'library_revision': str(library_revision)}

    def restore(self, saved, reference):
        """Restore as 'uncertain' only: needs one fresh verification before 'locked'."""
        if (self.reference is not None or not saved or not reference
                or reference.get('video_id') != saved.get('video_id')):
            return False
        duration = reference.get('duration_s')
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            return False
        now = self.clock()
        self.reference = reference
        self.state = 'uncertain'
        self.lock_id = str(saved.get('lock_id') or uuid.uuid4().hex)
        self.revision += 1
        self.position_s = (float(saved.get('position_s') or 0) + float(saved.get('age_s') or 0)) % duration
        self.anchor = now
        self.verified_at = now - float(saved.get('age_s') or 0)
        self.next_check = now
        self.previous = None
        return True

    def snapshot(self):
        if self.reference is None:
            return {'available': False, 'state': self.state}
        return {'available': self.state == 'locked', 'state': self.state,
                'video_id': self.reference['video_id'], 'lock_id': self.lock_id,
                'revision': self.revision, 'position_s': self.position(),
                'video_duration_s': self.reference['duration_s'],
                'verification_age_s': self.clock() - self.verified_at}
