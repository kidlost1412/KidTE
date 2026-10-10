import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image

path = Path(__file__).resolve().parents[2] / 'packaging' / 'solver-patches' / 'legacy_guard.py'
spec = importlib.util.spec_from_file_location('fixture_legacy_guard', path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize('change', ['none', 'box_moved', 'slider_moved', 'box_resized',
    'slider_resized', 'canvas_resized', 'image_changed', 'disappeared'])
def test_delayed_api_never_uses_stale_legacy_challenge(change):
    before_box = SimpleNamespace(left=10, top=20, width=100, height=60)
    before_button = SimpleNamespace(left=10, top=90, width=20, height=20)
    box = SimpleNamespace(**vars(before_box))
    button = SimpleNamespace(**vars(before_button))
    previous_canvas = Image.new('RGB', (100, 40), 'black')
    canvas = previous_canvas.copy()
    if change == 'box_moved': box.left += 20
    if change == 'slider_moved': button.top += 20
    if change == 'box_resized': box.width += 2
    if change == 'slider_resized': button.width += 2
    if change == 'canvas_resized': canvas = Image.new('RGB', (90, 40))
    if change == 'image_changed': canvas = Image.new('RGB', (100, 40), 'white')
    def find_box(screenshot):
        if change == 'disappeared': raise ValueError('challenge disappeared')
        return box
    args = (previous_canvas, before_box, before_button, Image.new('RGB', (200, 200)),
        find_box, lambda image: button, lambda image: canvas, ValueError)
    if change == 'none': guard.verify_legacy_puzzle(*args)
    else:
        with pytest.raises(ValueError): guard.verify_legacy_puzzle(*args)


def test_audited_patch_installer_is_idempotent_and_refuses_unknown_solver(tmp_path):
    installer_spec = importlib.util.spec_from_file_location('fixture_installer', path.parent / 'install_guard.py')
    installer = importlib.util.module_from_spec(installer_spec)
    installer_spec.loader.exec_module(installer)
    package = tmp_path / 'src' / 'tiktok_live_studio_captcha_solver'
    package.mkdir(parents=True)
    original = (path.parent / 'solver-before-legacy-guard.py.snapshot').read_bytes()
    (package / 'solver.py').write_bytes(original)
    first = installer.apply_guard(tmp_path)
    assert installer.apply_guard(tmp_path) == first
    assert (package / 'solver.before-legacy-guard.snapshot').read_bytes() == original
    assert (package / 'solver.py').read_bytes() == (path.parent / 'solver-after-legacy-guard.py.snapshot').read_bytes()
    (package / 'solver.py').write_text('unknown version', encoding='utf-8')
    with pytest.raises(ValueError, match='solver_source_not_audited'):
        installer.apply_guard(tmp_path)


@pytest.mark.parametrize('changed', [False, True])
def test_actual_patched_solver_function_checks_legacy_image_before_mouse(changed):
    import ast
    import logging
    import math
    tree = ast.parse((path.parent / 'solver-after-legacy-guard.py.snapshot').read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'solve_puzzle_captcha')
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'puzzle_drag_distance']
    box = SimpleNamespace(left=0, top=0, width=100, height=60)
    button = SimpleNamespace(left=0, top=90, width=20, height=20)
    before = Image.new('RGB', (200, 200), 'black')
    after = Image.new('RGB', (200, 200), 'white' if changed else 'black')
    moves = []
    class MouseBoundary(Exception): pass
    def mouse(*args):
        moves.append(args)
        raise MouseBoundary()
    def no_modern(image): raise ValueError('not modern')
    namespace = {'ApiClient': object, 'TemplateMatchNotFound': ValueError, 'math': math,
        'LOGGER': logging.getLogger('fixture'), 'log_image': lambda *args: None,
        'looks_blank': lambda image: False,
        'find_modern_puzzle_layout': no_modern,
        'find_puzzle_captcha_box': lambda image: box, 'find_slide_button_box': lambda image: button,
        'extract_puzzle_canvas': lambda image: image.crop((0, 0, 100, 40)),
        'extract_piece_from_puzzle': lambda image: image, 'draw_over_piece': lambda image: image,
        'pil_to_b64_string': lambda image: 'fixture', 'verify_legacy_puzzle': guard.verify_legacy_puzzle,
        'pyautogui': SimpleNamespace(screenshot=lambda: after, moveTo=mouse)}
    exec(compile(ast.Module(body=[*helpers, function], type_ignores=[]), 'audited_solver_fixture', 'exec'), namespace)
    api = SimpleNamespace(puzzle=lambda *args: SimpleNamespace(slide_x_proportion=.5))
    with pytest.raises(ValueError if changed else MouseBoundary):
        namespace['solve_puzzle_captcha'](api, screenshot=before)
    assert len(moves) == (0 if changed else 1)
