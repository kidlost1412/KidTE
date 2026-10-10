from ui_preferences import load_window_size, save_window_size


def test_destroyed_native_window_keeps_saved_size(tmp_path):
    path = tmp_path / "window.json"
    path.write_text('{"width":1400,"height":900}', encoding="utf-8")

    class DestroyedWindow:
        @property
        def width(self):
            raise TypeError("cannot unpack non-iterable NoneType object")

    assert save_window_size(path, DestroyedWindow()) is False
    assert load_window_size(path) == (1400, 900)
