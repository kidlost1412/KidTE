"""Thread-local pacing for automatic backup, never for foreground app requests."""
from contextlib import contextmanager
import threading
import time

_LOCAL = threading.local()


@contextmanager
def io_limit(bytes_per_second):
    previous = getattr(_LOCAL, 'budget', None)
    _LOCAL.budget = [float(bytes_per_second), time.monotonic(), 0]
    try:
        yield
    finally:
        _LOCAL.budget = previous


def account_io(count):
    budget = getattr(_LOCAL, 'budget', None)
    if budget is None:
        return
    budget[2] += count
    delay = budget[2] / budget[0] - (time.monotonic() - budget[1])
    if delay > 0:
        time.sleep(delay)
