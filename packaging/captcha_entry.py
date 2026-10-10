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
    configured_ffmpeg = os.environ.get("KIDTE_FFMPEG", "").strip()
    ffmpeg = Path(configured_ffmpeg) if configured_ffmpeg else None
    if ffmpeg is not None and not ffmpeg.is_file():
        ffmpeg = None
    if getattr(sys, "frozen", False):
        script_dir = Path(sys._MEIPASS) / "captcha-monitor"
        legacy_ffmpeg = Path(sys.executable).resolve().parent.parent / "tools" / "ffmpeg.exe"
    else:
        script_dir = Path(__file__).resolve().parent / "captcha-monitor"
        project = Path(__file__).resolve().parents[1]
        legacy_ffmpeg = project / "tools" / "ffmpeg" / "ffmpeg.exe"
    if ffmpeg is None:
        ffmpeg = legacy_ffmpeg
        if not ffmpeg.is_file():
            found = shutil.which("ffmpeg.exe")
            ffmpeg = Path(found) if found else ffmpeg
    script = script_dir / "monitor.ps1"
    if not script.is_file() or not ffmpeg.is_file():
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
    args = parser.parse_args()
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
