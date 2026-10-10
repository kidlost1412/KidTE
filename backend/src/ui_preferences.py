"""Tùy chọn cửa sổ theo máy, không chứa dữ liệu tài khoản."""
from __future__ import annotations

import json
from pathlib import Path


def load_window_size(path, default=(1280, 800)):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        width, height = int(value["width"]), int(value["height"])
        if 960 <= width <= 7680 and 640 <= height <= 4320:
            return width, height
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return default


def save_window_size(path, window):
    try:
        width, height = getattr(window, "width", None), getattr(window, "height", None)
    except (TypeError, RuntimeError, AttributeError):
        # The native handle may already be destroyed after webview.start returns.
        # Keep the saved dimensions and let normal shutdown finish.
        return False
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"width": max(960, width), "height": max(640, height)}),
                         encoding="utf-8")
    temporary.replace(path)
    return True
