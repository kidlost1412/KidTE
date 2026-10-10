import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

PATCHES = Path(__file__).resolve().parents[2] / 'packaging' / 'solver-patches'


def load(name):
    spec = importlib.util.spec_from_file_location('fixture_' + name, PATCHES / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


episode = load('episode_guard')


def test_seven_failed_attempts_alert_once_and_pause():
    guard = episode.EpisodeGuard()
    for index in range(6):
        guard.record_attempt(index * 10)
        assert guard.observe(True, index * 10 + 6) is None
    guard.record_attempt(60)
    assert guard.observe(True, 66) == 'manual'
    assert guard.paused
    assert guard.observe(True, 70) is None  # once per episode
    assert guard.observe(True, 500) is None  # still paused while visible


def test_captcha_gone_after_seventh_attempt_is_not_a_failure():
    guard = episode.EpisodeGuard()
    for index in range(7):
        guard.record_attempt(index)
    assert guard.observe(False, 8) is None
    assert not guard.paused
    assert guard.observe(False, 8 + episode.GAP_SECONDS) is None
    assert guard.attempts == 0


def test_operator_solve_resumes_normal_behaviour_with_fresh_count():
    guard = episode.EpisodeGuard()
    for index in range(7):
        guard.record_attempt(index)
    assert guard.observe(True, 10) == 'manual'
    assert guard.observe(False, 12) is None and guard.paused  # brief flicker
    assert guard.observe(True, 14) is None and guard.paused
    assert guard.observe(False, 20) is None and guard.paused
    assert guard.observe(False, 20 + episode.GAP_SECONDS) is None
    assert not guard.paused and guard.attempts == 0
    guard.record_attempt(100)
    assert guard.observe(True, 105) is None  # new episode, 1 attempt


def test_brief_gap_between_attempts_keeps_the_episode():
    guard = episode.EpisodeGuard()
    guard.record_attempt(0)
    guard.observe(False, 5)
    assert guard.attempts == 1


def test_captcha_visible_uses_any_finder():
    class Missing(Exception): pass
    def absent(image): raise Missing()
    assert episode.captcha_visible(None, (absent, lambda image: 1), Missing)
    assert not episode.captcha_visible(None, (absent, absent), Missing)


def _solve_loop(captcha_visible_sequence, monkeypatch_sleep_limit=200):
    """Run the real solve_loop from the audited snapshot with fakes."""
    tree = ast.parse((PATCHES / 'solver-after-legacy-guard.py.snapshot').read_text(encoding='utf-8'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'solve_loop')
    clock = {'now': 0.0, 'steps': 0}
    drags, records = [], []
    class Missing(Exception): pass
    class Stop(BaseException): pass
    visible = iter(captcha_visible_sequence)
    state = {'visible': True}
    def sleep(seconds):
        clock['now'] += seconds
        clock['steps'] += 1
        if clock['steps'] > monkeypatch_sleep_limit: raise Stop()
        try: state['visible'] = next(visible)
        except StopIteration: raise Stop()
    guard = episode.EpisodeGuard()
    def finder(image):
        if not state['visible']: raise Missing()
    def solve_puzzle(api, screenshot=None):
        if not state['visible']: raise Missing()
        drags.append(clock['now'])
        guard.record_attempt(clock['now'])
    class Log:
        def info(self, *a): pass
        def debug(self, *a): pass
        def exception(self, *a): pass
        def error(self, *a): pass
        def warning(self, fmt, *a): records.append(fmt % a)
    namespace = {'pyautogui': SimpleNamespace(screenshot=lambda: SimpleNamespace(size=(1, 1))),
        'time': SimpleNamespace(monotonic=lambda: clock['now'], sleep=sleep),
        'LOGGER': Log(), 'EPISODE': guard, 'MANUAL_MARKER': episode.MANUAL_MARKER,
        'captcha_visible': episode.captcha_visible,
        'find_modern_puzzle_layout': finder, 'find_puzzle_captcha_box': finder,
        'find_shapes_captcha_box': finder, 'TemplateMatchNotFound': Missing,
        'solve_puzzle_captcha': solve_puzzle, 'solve_shapes_captcha': solve_puzzle,
        'ApiAuthError': type('A', (Exception,), {}), 'ApiResponseError': type('R', (Exception,), {}),
        'ApiClient': object}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'audited_solver_fixture', 'exec'), namespace)
    with pytest.raises(Stop):
        namespace['solve_loop'](None)
    return drags, records, guard


def test_real_solve_loop_never_drags_an_eighth_time_and_alerts_once():
    drags, records, guard = _solve_loop([True] * 60)
    assert len(drags) == 7
    assert len(records) == 1 and episode.MANUAL_MARKER in records[0] and '7 attempts' in records[0]
    assert guard.paused


def test_real_solve_loop_resumes_after_operator_clears_captcha():
    sequence = [True] * 12 + [False] * 40 + [True]
    drags, records, guard = _solve_loop(sequence)
    assert len(records) == 1
    assert len(drags) == 8  # 7, pause, captcha gone >=15 s, one fresh attempt
    assert guard.attempts == 1 and not guard.paused


def test_installer_ships_episode_guard_and_accepts_previous_patched_solver(tmp_path):
    installer = load('install_guard')
    manifest = json.loads((PATCHES / 'manifest.json').read_text(encoding='utf-8'))
    import hashlib
    assert hashlib.sha256((PATCHES / 'solver-after-legacy-guard.py.snapshot').read_bytes()).hexdigest() == manifest['solver_after_sha256']
    assert hashlib.sha256((PATCHES / 'episode_guard.py').read_bytes().replace(b'\r\n', b'\n')).hexdigest() == manifest['episode_guard_sha256']
    package = tmp_path / 'src' / 'tiktok_live_studio_captcha_solver'
    package.mkdir(parents=True)
    assert manifest['solver_prior_after_sha256']
    (package / 'solver.py').write_bytes((PATCHES / 'solver-before-legacy-guard.py.snapshot').read_bytes())
    installer.apply_guard(tmp_path)
    assert (package / 'episode_guard.py').is_file()
    assert (package / 'solver.py').read_bytes() == (PATCHES / 'solver-after-legacy-guard.py.snapshot').read_bytes()


def test_monitor_alerts_on_the_marker_with_operator_text_and_spec_packages_module():
    root = PATCHES.parent
    monitor = (root / 'captcha-monitor' / 'monitor.ps1').read_text(encoding='utf-8-sig')
    assert episode.MANUAL_MARKER in monitor
    assert 'đã thử 7 lần chưa qua — vào giải tay' in monitor
    assert 'rejected the API key' in monitor  # HTTP 401 alert
    spec = (root / 'KidTECaptcha.spec').read_text(encoding='utf-8')
    assert 'episode_guard' in spec
