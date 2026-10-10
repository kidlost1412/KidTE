"""Stop auto-solving a TikTok CAPTCHA episode after too many failed attempts.

An episode starts with the first mouse attempt and ends when no CAPTCHA has
been visible for GAP_SECONDS. Counting and pausing only; mouse motion is
untouched. The solver logs MANUAL_MARKER once; the Telegram monitor matches it.
"""
MAX_ATTEMPTS = 7
GAP_SECONDS = 15.0
MANUAL_MARKER = 'CAPTCHA_MANUAL_REQUIRED'


class EpisodeGuard:
    def __init__(self, max_attempts=MAX_ATTEMPTS, gap_seconds=GAP_SECONDS):
        self.max_attempts = max_attempts
        self.gap_seconds = gap_seconds
        self.reset()

    def reset(self):
        self.attempts = 0
        self.paused = False
        self.last_visible = None

    def record_attempt(self, now):
        self.attempts += 1
        self.last_visible = now

    def observe(self, visible, now):
        """Feed one screen observation. Returns 'manual' exactly once per
        episode, when the limit is reached and the CAPTCHA is still there."""
        if not self.attempts:
            return None
        if visible:
            self.last_visible = now
            if not self.paused and self.attempts >= self.max_attempts:
                self.paused = True
                return 'manual'
            return None
        if self.last_visible is None or now - self.last_visible >= self.gap_seconds:
            self.reset()
        return None


def captcha_visible(screenshot, finders, not_found_error):
    for finder in finders:
        try:
            finder(screenshot)
            return True
        except not_found_error:
            continue
    return False
