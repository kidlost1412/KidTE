"""Frozen entry point of the ``runtime`` package.

The runtime holds only the interpreter and third-party libraries. Kid TE's own
code lives in the separately updated ``app`` package, which the launcher passes in
``KIDTE_APP_PACKAGE_DIR`` after verifying it. Without that variable (installer
baseline, direct start) the app sits next to ``KidTE.exe``.

Keep this file tiny and stable: changing it changes the runtime package.
"""
import os
import sys
from pathlib import Path


def _fail(message):
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Kid TE không khởi động được", 0x10 | 0x1000)
        except Exception:  # noqa: BLE001 - no UI available
            pass
    print(f"[kid-te] {message}", file=sys.stderr)
    return 3


def main():
    explicit = os.environ.get("KIDTE_APP_PACKAGE_DIR", "").strip()
    app_dir = Path(explicit) if explicit else Path(sys.executable).resolve().parent
    source = app_dir / "backend" / "src"
    if not (source / "app_entry.py").is_file():
        return _fail(f"Không tìm thấy mã ứng dụng Kid TE trong {app_dir}")
    # Immutable packages must never gain files (their inventory is verified on every
    # activation): never write bytecode next to the sources. Compiling ~3 MB takes ~0.3 s.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    import app_entry

    return app_entry.main()


if __name__ == "__main__":
    raise SystemExit(main())
