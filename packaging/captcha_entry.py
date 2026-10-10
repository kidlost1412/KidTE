"""Independent CAPTCHA executable: only setup, key loading and singleton guard.

All screenshot recognition, SadCaptcha requests and mouse actions remain inside
the locally patched tiktok_live_studio_captcha_solver package.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))
from captcha_secret import has_key, has_telegram, load_key


def _start_monitor(data_dir: Path):
    from captcha_upload import upload_worker
    threading.Thread(target=upload_worker, args=(data_dir,), name='captcha-upload', daemon=True).start()
    if not has_telegram(data_dir):
        return
    ffmpeg = _ffmpeg_binary()
    if getattr(sys, "frozen", False):
        script_dir = Path(sys._MEIPASS) / "captcha-monitor"
    else:
        script_dir = Path(__file__).resolve().parent / "captcha-monitor"
    script = script_dir / "monitor.ps1"
    if not script.is_file() or ffmpeg is None:
        (data_dir / "monitor-error.txt").write_text(
            "Telegram monitor or FFmpeg is missing.", encoding="utf-8")
        return
    # PowerShell cannot run -File with DETACHED_PROCESS; it exits silently.
    flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
             getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", str(script), "-DataDir", str(data_dir),
                      "-Ffmpeg", str(ffmpeg), "-SolverPid", str(os.getpid())], cwd=data_dir,
                     creationflags=flags, close_fds=True)


def _derive_app_root(executable: Path) -> Path | None:
    """Return the install root only for a recognized exact solver layout."""
    resolved = executable.resolve()
    parts = resolved.parts
    expected_prefix = ("packages", "captcha-solver")
    if len(parts) >= 5:
        # Versioned optional package: App/packages/captcha-solver/<version>/Captcha/KidTECaptcha.exe
        actual_prefix = tuple(part.casefold() for part in parts[-5:-3])
        actual_suffix = tuple(part.casefold() for part in parts[-2:])
        if (actual_prefix == expected_prefix and parts[-3]
                and actual_suffix == ("captcha", "kidtecaptcha.exe")):
            return Path(*parts[:-5])
    if len(parts) >= 2 and tuple(part.casefold() for part in parts[-2:]) == (
            "captcha", "kidtecaptcha.exe"):
        # Legacy install: App/Captcha/KidTECaptcha.exe.
        return Path(*parts[:-2])
    return None


def _initialize_app_root() -> Path | None:
    """Recover the install root after UAC drops the parent process environment."""
    if not getattr(sys, "frozen", False):
        configured = os.environ.get("KIDTE_APP_ROOT", "").strip()
        return Path(configured).resolve() if configured else None
    derived = _derive_app_root(Path(sys.executable))
    configured = os.environ.get("KIDTE_APP_ROOT", "").strip()
    if derived is None:
        return Path(configured).resolve() if configured else None
    if configured and Path(configured).resolve() != derived:
        raise RuntimeError("captcha_app_root_mismatch")
    os.environ["KIDTE_APP_ROOT"] = str(derived)
    return derived


def _selected_media_ffmpeg() -> Path | None:
    """Resolve the active signed media package; corruption must remain visible."""
    if not getattr(sys, "frozen", False) and not os.environ.get("KIDTE_APP_ROOT", "").strip():
        return None
    from component_runtime import component_path
    selected = component_path("media-tools", "tools/ffmpeg.exe")
    return Path(selected) if selected is not None else None


def _ffmpeg_binary() -> Path | None:
    _initialize_app_root()
    # Validate the selected package before honoring an explicit path: a stale or
    # damaged active package must not be masked by a legacy copy or PATH install.
    selected = _selected_media_ffmpeg()
    configured = os.environ.get("KIDTE_FFMPEG", "").strip()
    if configured:
        explicit = Path(configured)
        if not explicit.is_file():
            raise RuntimeError("selected_media_tool_missing:ffmpeg")
        return explicit
    if selected is not None:
        return selected
    if getattr(sys, "frozen", False):
        app_root = _initialize_app_root()
        legacy = (app_root / "tools" / "ffmpeg.exe") if app_root else None
    else:
        project = Path(__file__).resolve().parents[1]
        legacy = project / "tools" / "ffmpeg" / "ffmpeg.exe"
    if legacy is not None and legacy.is_file():
        return legacy
    found = shutil.which("ffmpeg.exe")
    return Path(found) if found else None


def _resource_count() -> int:
    import importlib.util
    spec = importlib.util.find_spec("tiktok_live_studio_captcha_solver")
    if spec is None or not spec.submodule_search_locations:
        return 0
    root = Path(next(iter(spec.submodule_search_locations))) / "resources"
    return sum(1 for item in root.rglob("*") if item.is_file()) if root.is_dir() else 0


def package_probe() -> dict:
    """Check packaged resources without touching data, keys, capture, or solver APIs."""
    count = 0
    app_root = None
    try:
        app_root = _initialize_app_root()
        count = _resource_count()
        ffmpeg = _ffmpeg_binary()
        return {"ok": count > 0 and ffmpeg is not None,
                "resource_count": count, "ffmpeg": str(ffmpeg) if ffmpeg else None,
                "app_root": str(app_root) if app_root else None}
    except Exception as error:  # noqa: BLE001 - the offline diagnostic must return structured failure.
        return {"ok": False, "resource_count": count, "ffmpeg": None,
                "app_root": str(app_root) if app_root else None,
                "error": f"{type(error).__name__}: {error}"[:200]}


def _single_instance():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, True, "Local\\KidTECaptchaSolver")
    if not handle:
        raise OSError("Could not create CAPTCHA process mutex")
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def _check(name, func):
    try:
        ok, detail, hint = func()
    except Exception as error:   # a check must never abort the whole report
        ok, detail, hint = False, f"{type(error).__name__}: {error}"[:200], "Xem chi tiết lỗi."
    return {"name": name, "ok": bool(ok), "detail": detail, "hint": "" if ok else hint}


def selftest(data_dir: Path | None) -> dict:
    """Read-only environment checks. Needs neither Administrator nor a key."""
    def solver_import():
        import importlib
        importlib.import_module("tiktok_live_studio_captcha_solver.solver")
        return True, "Đã nạp gói bộ giải", ""

    def resources():
        import importlib.util
        spec = importlib.util.find_spec("tiktok_live_studio_captcha_solver")
        root = Path(list(spec.submodule_search_locations)[0]) / "resources"
        count = sum(1 for item in root.rglob("*") if item.is_file()) if root.is_dir() else 0
        return count > 0, f"{count} tệp tài nguyên", "Cài lại Kid TE: thiếu tài nguyên nhận dạng."

    def ffmpeg():
        configured = os.environ.get("KIDTE_FFMPEG", "").strip()
        path = Path(configured) if configured else None
        if path is not None and path.is_file():
            return True, str(path), ""
        if getattr(sys, "frozen", False):
            path = Path(sys.executable).resolve().parent.parent / "tools" / "ffmpeg.exe"
        else:
            path = Path(__file__).resolve().parents[1] / "tools" / "ffmpeg" / "ffmpeg.exe"
        found = path if path.is_file() else shutil.which("ffmpeg.exe")
        return bool(found), str(found or "không thấy"), "Cài lại Kid TE: thiếu ffmpeg để quay video."

    def key_decrypt():
        if data_dir is None or not has_key(data_dir):
            return True, "Chưa có key (bỏ qua)", ""
        return bool(load_key(data_dir)), "Giải mã được key trên tài khoản Windows này", ""

    def capture():
        from PIL import ImageGrab
        image = ImageGrab.grab(all_screens=True)
        monitors = ctypes.windll.user32.GetSystemMetrics(80)
        primary = ImageGrab.grab()
        return (image.width > 0 and image.height > 0,
                f"{primary.width}x{primary.height}, {monitors} màn hình",
                "Mở khóa màn hình / chạy trong phiên đăng nhập đang hiển thị.")

    def tls():
        import requests
        response = requests.get("https://www.sadcaptcha.com/", timeout=8)
        return response.status_code < 500, f"HTTP {response.status_code}",             "Kiểm tra mạng, VPN hoặc tường lửa chặn sadcaptcha.com."
    checks = [_check("solver_import", solver_import), _check("resources", resources),
              _check("ffmpeg", ffmpeg), _check("key_decrypt", key_decrypt),
              _check("screen_capture", capture), _check("sadcaptcha_tls", tls)]
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--owner-pid", type=int, default=0)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--package-probe", action="store_true")
    args = parser.parse_args()
    if args.package_probe:
        report = package_probe()
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if args.selftest:
        report = selftest(args.data_dir.resolve() if args.data_dir else None)
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if args.data_dir is None:
        parser.error("--data-dir is required")
    data_dir = args.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    # Keep a handle to the original owner: a recycled PID cannot keep us alive.
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    owner = kernel.OpenProcess(0x100000, False, args.owner_pid) if args.owner_pid else None
    if args.owner_pid and not owner:
        return 0
    if (data_dir / "stop-requested.marker").exists():
        return 0
    os.chdir(data_dir)
    # Chẩn đoán: có file log-images.on thì bộ giải lưu ảnh đã cắt/gửi (ghi đè cùng tên,
    # không phình ổ đĩa). Phải đặt trước khi import gói solver (logs.py đọc lúc import).
    if (data_dir / "log-images.on").exists():
        os.environ["LOG_IMAGES"] = "1"
    mutex = _single_instance()
    if mutex is None:
        return 0
    if not bool(ctypes.windll.shell32.IsUserAnAdmin()):
        (data_dir / "startup-error.txt").write_text(
            "Solver requires Administrator permission.", encoding="utf-8")
        return 3
    try:
        api_key = load_key(data_dir)
    except (OSError, UnicodeError) as error:
        (data_dir / "startup-error.txt").write_text(
            f"Could not load SadCaptcha key: {type(error).__name__}", encoding="utf-8")
        return 4
    (data_dir / "startup-error.txt").unlink(missing_ok=True)
    (data_dir / "solver-session.json").write_text(json.dumps({
        "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(),
        "admin": True,
    }), encoding="utf-8")
    monitor = None
    try:
        monitor = _start_monitor(data_dir)
    except Exception as error:
        (data_dir / "monitor-error.txt").write_text(
            f"Could not start Telegram monitor: {type(error).__name__}",
            encoding="utf-8")
    def watch_owner():
        while True:
            if (data_dir / "stop-requested.marker").exists() or (owner and kernel.WaitForSingleObject(owner, 0) != 258):
                try:
                    if monitor is not None and monitor.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(monitor.pid), "/T", "/F"],
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                       capture_output=True, timeout=5, check=False)
                finally:
                    os._exit(0)
            threading.Event().wait(0.2)
    threading.Thread(target=watch_owner, daemon=True, name="kid-te-owner-watch").start()
    try:
        from tiktok_live_studio_captcha_solver.api import ApiClient
        from tiktok_live_studio_captcha_solver.solver import solve_loop
        solve_loop(ApiClient(api_key))
    except SystemExit as error:
        if error.code == 2:
            (data_dir / "key-rejected.marker").write_text(json.dumps({
                "key_sha256": hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
            }), encoding="utf-8")
        return int(error.code or 0)
    except Exception as error:
        (data_dir / "startup-error.txt").write_text(
            f"Solver stopped: {type(error).__name__}", encoding="utf-8")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
