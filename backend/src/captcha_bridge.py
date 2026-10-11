"""Launch and observe a separate CAPTCHA program without touching its solver."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import psutil
import requests
import sqlite3
from contextlib import closing
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import captcha_health
from captcha_secret import has_key, has_telegram, load_key, load_telegram, save_key, save_telegram
from config import INSTALL_ROOT, IS_FROZEN, PROJECT_ROOT, ROOT, find_tool


class CaptchaBridge:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir or ROOT / "captcha")
        self._lock = threading.Lock()
        self._closing = False

    def _command(self):
        if IS_FROZEN:
            exe = self._solver_executable()
            return [str(exe), "--data-dir", str(self.data_dir)]
        solver_root = Path(os.environ.get("KIDTE_CAPTCHA_SOLVER_ROOT", "").strip()
                           or PROJECT_ROOT.parent.parent / "TikTokCaptchaSolver-clean-20260926")
        python = solver_root / "venv" / "Scripts" / "pythonw.exe"
        entry = PROJECT_ROOT / "packaging" / "captcha_entry.py"
        return [str(python), str(entry), "--data-dir", str(self.data_dir)]

    @staticmethod
    def _selected_solver_executable():
        if not IS_FROZEN:
            return None
        # A selected optional package is authoritative. The resolver raises
        # for a corrupt selected package; only a genuinely absent package may
        # fall back to the legacy App\Captcha location.
        from component_runtime import component_path
        return component_path("captcha-solver", "Captcha/KidTECaptcha.exe")

    def _solver_executable(self):
        selected = self._selected_solver_executable()
        if selected is not None:
            return Path(selected)
        return INSTALL_ROOT / "Captcha" / "KidTECaptcha.exe"

    def _solver_package_root(self):
        selected = self._selected_solver_executable()
        if selected is not None:
            return Path(selected).parent
        return INSTALL_ROOT / "Captcha"

    @staticmethod
    def _ffmpeg_path():
        found = find_tool("ffmpeg")
        return Path(found) if found else None

    def _solver_environment(self):
        env = {**os.environ, "PYTHONUTF8": "1"}
        ffmpeg = self._ffmpeg_path()
        if ffmpeg is not None:
            env["KIDTE_FFMPEG"] = str(ffmpeg.resolve())
        else:
            env.pop("KIDTE_FFMPEG", None)
        return env

    def _processes(self):
        # Lọc theo tên trước, chỉ đọc cmdline của vài tiến trình khớp: đọc cmdline
        # của mọi tiến trình làm psutil chờ thử lại với tiến trình được bảo vệ (~2s/lần).
        own = []
        external = []
        own_data = os.path.normcase(os.path.abspath(self.data_dir))
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").casefold() not in {
                    "python.exe", "pythonw.exe", "kidtecaptcha.exe"}:
                    continue
                args = [str(part).casefold() for part in (proc.cmdline() or [])]
                if any(arg.endswith("kidtecaptcha.exe") or
                       arg.endswith("captcha_entry.py") for arg in args):
                    try:
                        index = args.index("--data-dir")
                        data_arg = args[index + 1]
                    except (ValueError, IndexError):
                        data_arg = next((arg.split("=", 1)[1] for arg in args
                                         if arg.startswith("--data-dir=")), "")
                    if data_arg and os.path.normcase(os.path.abspath(data_arg)) == own_data:
                        own.append(proc)
                    else:
                        external.append(proc)
                elif "tiktok_live_studio_captcha_solver" in args:
                    external.append(proc)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        # Windows can hide an elevated process's command line from Kid TE.
        # The worker records its PID and start time in this data directory.
        session = self._active_session()
        if session and session["pid"] not in {proc.pid for proc in own}:
            try:
                own.append(psutil.Process(session["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        return own, external

    def _monitor_pid(self):
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").casefold() not in {"powershell.exe", "pwsh.exe"}:
                    continue
                args = [str(item).casefold() for item in (proc.cmdline() or [])]
                if (any(item.endswith("captcha-monitor\\monitor.ps1") for item in args)
                        and str(self.data_dir).casefold() in args):
                    return proc.pid
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return None

    @staticmethod
    def _tail_text(path: Path, byte_limit: int) -> str:
        # The solver and monitor write independently; a log may be rotated or
        # removed between two filesystem calls during a dashboard poll.
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - byte_limit))
                return stream.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _read_status_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")[:200]
        except (OSError, UnicodeError):
            return ""

    def _events(self, limit=30):
        path = self.data_dir / "tiktok-captcha-solver-logs" / "ttls-captcha-solver.log"
        raw = self._tail_text(path, 96_000)
        entries = []
        for line in raw.splitlines():
            if " | " not in line or not any(word in line for word in (
                "captcha detected", "got response", "dragged puzzle",
                "puzzle still visible", "puzzle no longer visible",
                "puzzle geometry", "answer unavailable", "solver stopped", "clicked",
                "unexpected exception", "could not capture screen", "scanning screen",
            )):
                continue
            if "scanning screen" in line and entries and "scanning screen" in entries[-1]["detail"]:
                entries[-1] = {"at": line[:19], "detail": line.split(" | ", 3)[-1][:300]}
                continue
            entries.append({"at": line[:19], "detail": line.split(" | ", 3)[-1][:300]})
        return entries[-limit:]

    def status(self):
        package_error = None
        try:
            command = self._command()
            available = Path(command[0]).is_file()
        except Exception:
            # A corrupt selected package is not treated as absent for path
            # resolution, but status endpoints must remain usable so the UI
            # can explain how to repair it.
            command = None
            available = False
            package_error = "captcha_solver_package_invalid"
        own, external = self._processes()
        session = self._active_session()
        events = self._events()
        native_log = self.data_dir / "tiktok-captcha-solver-logs" / "ttls-captcha-solver.log"
        try:
            log_age_seconds = max(0, int(time.time() - native_log.stat().st_mtime))
        except OSError:
            log_age_seconds = None
        error_path = self.data_dir / "startup-error.txt"
        error = self._read_status_text(error_path)
        monitor_log = self.data_dir / "telegram-monitor.log"
        monitor_events = self._tail_text(monitor_log, 16_000).splitlines()[-8:]
        monitor_error = self.data_dir / "monitor-error.txt"
        telegram_configured = has_telegram(self.data_dir)
        chat_hint = ""
        telegram_error = ""
        if telegram_configured:
            try:
                token, chat_id = load_telegram(self.data_dir)
                chat_hint = "••••" + chat_id[-4:]
                if chat_id == token.split(":", 1)[0]:
                    telegram_error = "captcha_telegram_chat_is_bot"
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                telegram_error = "captcha_telegram_unreadable"
        return {
            "available": available,
            "package_error": package_error,
            "configured": has_key(self.data_dir),
            "telegram_configured": telegram_configured,
            "telegram_chat_hint": chat_hint,
            "telegram_error": telegram_error,
            "monitor_pid": self._monitor_pid(),
            "monitor_events": monitor_events,
            "video_uploads": self._upload_status(),
            "monitor_error": self._read_status_text(monitor_error),
            "running": bool(own) or session is not None,
            "pid": session["pid"] if session else (own[0].pid if own else None),
            "external_solver_running": bool(external),
            "external_pid": external[0].pid if external else None,
            "admin": bool(session and session["admin"]),
            "key_rejected": self._key_rejected(),
            "error": package_error or error,
            "events": events,
            "log_age_seconds": log_age_seconds,
            "health": self.health(running=bool(own) or session is not None,
                                  available=available),
        }

    def _read_start_error(self):
        try:
            data = json.loads((self.data_dir / "last-start.json").read_text(encoding="utf-8"))
            return str(data.get("message") or data.get("error") or "")[:200] or None
        except (OSError, ValueError, AttributeError):
            return None

    def _record_start(self, result):
        path = self.data_dir / "last-start.json"
        try:
            if result.get("ok"):
                path.unlink(missing_ok=True)
                return
            error = str(result.get("error") or "captcha_start_failed")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "error": error,
                "message": captcha_health.START_REASONS.get(error, error)}), encoding="utf-8")
        except OSError:
            pass

    def health(self, *, running=None, available=None, now=None):
        """Real health from the solver log, not just "process exists"."""
        now = now or datetime.now()
        if running is None or available is None:
            own, _ = self._processes()
            running = bool(own) or self._active_session() is not None
            try:
                available = Path(self._command()[0]).is_file()
            except Exception:
                available = False
        folder = self.data_dir / "tiktok-captcha-solver-logs"
        info = captcha_health.parse_log(
            self._tail_text(folder / "ttls-captcha-solver.log", 96_000), now.strftime("%Y-%m-%d"))
        info["last_telegram_ok_at"] = captcha_health.parse_monitor(
            self._tail_text(self.data_dir / "telegram-monitor.log", 64_000))
        started = None
        try:
            started = datetime.fromisoformat(json.loads(
                (self.data_dir / "solver-session.json").read_text(encoding="utf-8"))["started_at"]
            ).astimezone().replace(tzinfo=None)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        start_error = self._read_start_error() or self._read_status_text(
            self.data_dir / "startup-error.txt") or None
        verdict = captcha_health.derive(
            available=available, configured=has_key(self.data_dir),
            key_rejected=self._key_rejected(), running=running, start_error=start_error,
            info=info, now=now, started_at=started)
        return {**verdict, **info, "last_result_text": captcha_health.result_text(info),
                "checked_at": now.strftime("%Y-%m-%d %H:%M:%S")}

    def _upload_status(self):
        from captcha_upload import MAX_BYTES
        folder = self.data_dir / 'screen-recordings'
        try:
            bytes_used = sum(path.stat().st_size for path in folder.glob('screen-*.mp4') if path.is_file())
        except OSError:
            return {'available': False, 'error': 'recording_storage_unreadable'}
        result = {'available': True, 'bytes_used': bytes_used, 'max_bytes': MAX_BYTES,
                  'recording_paused': bytes_used + 256 * 1024 ** 2 > MAX_BYTES,
                  'pending': 0, 'sending': 0}
        database = self.data_dir / 'captcha-uploads.sqlite3'
        if database.is_file():
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                    for state, count in db.execute('SELECT state,COUNT(*) FROM uploads GROUP BY state'):
                        if state in {'pending', 'sending'}: result[state] = count
            except sqlite3.Error:
                result.update(available=False, error='upload_queue_unreadable')
        return result

    def close_uploads(self):
        """Close a held uploads store, if any (the bridge opens it per call, so usually a no-op)."""
        store = getattr(self, 'uploads', None)
        close = getattr(store, 'close', None)
        if callable(close):
            close()

    def _active_session(self):
        path = self.data_dir / "solver-session.json"
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            pid = int(session["pid"])
            process = psutil.Process(pid)
            if not process.is_running() or process.name().casefold() not in {
                    "python.exe", "pythonw.exe", "kidtecaptcha.exe"}:
                return None
            started = datetime.fromisoformat(session["started_at"]).timestamp()
            if abs(process.create_time() - started) > 30:
                return None
            return {"pid": pid, "admin": session.get("admin") is True}
        except (OSError, ValueError, TypeError, KeyError, psutil.Error):
            return None

    def _key_rejected(self):
        marker = self.data_dir / "key-rejected.marker"
        if not marker.is_file():
            return False
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(recorded, dict) or not recorded.get("key_sha256"):
                return True
            current = hashlib.sha256(load_key(self.data_dir).encode("utf-8")).hexdigest()
            return recorded.get("key_sha256") == current
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            # Markers written by older versions must still stop retries.
            return True

    def _monitor_command(self, solver_pid):
        if IS_FROZEN:
            script = self._solver_package_root() / "_internal" / "captcha-monitor" / "monitor.ps1"
        else:
            script = PROJECT_ROOT / "packaging" / "captcha-monitor" / "monitor.ps1"
        ffmpeg = self._ffmpeg_path()
        if not script.is_file() or ffmpeg is None or not ffmpeg.is_file():
            return None
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-DataDir", str(self.data_dir),
                "-Ffmpeg", str(ffmpeg), "-SolverPid", str(solver_pid)]

    def _ensure_monitor(self, state):
        if state.get("package_error"):
            return {"monitor_start_requested": False,
                    "monitor_error": state["package_error"]}
        if (not state["running"] or not state["telegram_configured"]
                or state.get("telegram_error") or state["monitor_pid"]):
            return {"monitor_start_requested": False}
        try:
            command = self._monitor_command(state["pid"])
        except Exception:
            return {"monitor_start_requested": False,
                    "monitor_error": "captcha_solver_package_invalid"}
        if command is None:
            return {"monitor_start_requested": False, "monitor_error": "captcha_monitor_missing"}
        # Windows PowerShell exits before running -File under DETACHED_PROCESS.
        # CREATE_NO_WINDOW keeps the observer invisible while allowing its
        # console host to initialize normally.
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                 getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            subprocess.Popen(command, cwd=self.data_dir, creationflags=flags,
                             close_fds=True, env=self._solver_environment())
        except OSError:
            return {"monitor_start_requested": False, "monitor_error": "captcha_monitor_start_failed"}
        return {"monitor_start_requested": True}

    def start(self):
        """Start the solver and persist any failure so health() can explain it."""
        try:
            result = self._start()
        except OSError:
            result = {"ok": False, "error": "captcha_start_failed"}
        self._record_start(result)
        return result

    def _start(self):
        with self._lock:
            if self._closing:
                return {"ok": False, "error": "captcha_app_closing"}
            state = self.status()
            if state.get("package_error"):
                return {**state, "ok": False, "error": state["package_error"]}
            if state["running"]:
                return {**state, "ok": True, "already_running": True,
                        **self._ensure_monitor(state)}
            if state["external_solver_running"]:
                return {**state, "ok": False, "error": "captcha_other_solver_running"}
            if not state["available"]:
                return {**state, "ok": False, "error": "captcha_solver_missing"}
            if not state["configured"]:
                return {**state, "ok": False, "error": "captcha_key_missing"}
            if state["key_rejected"]:
                return {**state, "ok": False, "error": "captcha_key_rejected"}
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "stop-requested.marker").unlink(missing_ok=True)
            command = self._command() + ["--owner-pid", str(os.getpid())]
            if bool(ctypes.windll.shell32.IsUserAnAdmin()):
                flags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                         getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                         getattr(subprocess, "CREATE_NO_WINDOW", 0))
                subprocess.Popen(command, cwd=self.data_dir, creationflags=flags,
                                 close_fds=True, env=self._solver_environment())
            else:
                shell_execute = ctypes.windll.shell32.ShellExecuteW
                shell_execute.restype = ctypes.c_void_p
                result = shell_execute(
                    None, "runas", command[0],
                    subprocess.list2cmdline(command[1:]), str(self.data_dir), 0)
                if result is None or result <= 32:
                    return {"ok": False, "error": "captcha_admin_declined"}
            return {"ok": True, "starting": True}

    def selftest(self, timeout=45):
        """Run the solver executable's --selftest and return its parsed JSON."""
        try:
            if IS_FROZEN:
                command = self._command()[:1]
            else:
                python = Path(self._command()[0]).with_name("python.exe")   # console build, has solver deps
                command = [str(python), str(PROJECT_ROOT / "packaging" / "captcha_entry.py")]
        except Exception:
            return {"ok": False, "error": "captcha_solver_package_invalid", "checks": []}
        command += ["--selftest", "--data-dir", str(self.data_dir)]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace",
                                  env=self._solver_environment(),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "captcha_selftest_timeout", "checks": []}
        except OSError:
            error = ("captcha_solver_missing"
                     if IS_FROZEN and not Path(command[0]).is_file()
                     else "captcha_selftest_unavailable")
            return {"ok": False, "error": error, "checks": []}
        for line in reversed((done.stdout or "").strip().splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("checks"), list):
                return {"ok": bool(data.get("ok")) and done.returncode == 0,
                        "checks": data["checks"]}
        return {"ok": False, "error": "captcha_selftest_failed", "checks": []}

    def save_key(self, key):
        save_key(self.data_dir, key)
        (self.data_dir / "key-rejected.marker").unlink(missing_ok=True)
        (self.data_dir / "startup-error.txt").unlink(missing_ok=True)
        if self.status()["running"]:
            return {"ok": True, "restart_required": True}
        launch = self.start()
        return {"ok": True, "start_requested": launch.get("ok", False),
                "start_error": launch.get("error")}

    def save_telegram(self, token, chat_id):
        if not token.strip():
            try:
                token, _ = load_telegram(self.data_dir)
            except (OSError, ValueError, KeyError, UnicodeError):
                raise ValueError("captcha_telegram_missing") from None
        save_telegram(self.data_dir, token, chat_id)
        with self._lock:
            state = self.status()
            return {"ok": True, **self._ensure_monitor(state)}

    def test_telegram(self):
        try:
            token, chat_id = load_telegram(self.data_dir)
        except (OSError, ValueError, KeyError, UnicodeError):
            return {"ok": False, "error": "captcha_telegram_missing"}
        if chat_id == token.split(":", 1)[0]:
            return {"ok": False, "error": "captcha_telegram_chat_is_bot"}
        try:
            response = requests.post(
                "https://api.telegram.org/bot" + token + "/sendMessage",
                data={"chat_id": chat_id,
                      "text": "Kid TE: Kiểm tra kết nối Telegram. Đây không phải cảnh báo CAPTCHA."},
                timeout=12,
            )
        except requests.RequestException:
            return {"ok": False, "error": "captcha_telegram_network_error"}
        if not response.ok:
            return {"ok": False, "error": "captcha_telegram_chat_unreachable",
                    "http_status": response.status_code}
        try:
            accepted = response.json().get("ok") is True
        except (ValueError, AttributeError):
            accepted = False
        return {"ok": accepted, "error": None if accepted else "captcha_telegram_rejected"}

    def stop(self, *, closing=False):
        with self._lock:
            if closing:
                self._closing = True
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # The elevated solver can read this and stop itself without UAC.
            (self.data_dir / "stop-requested.marker").touch()
        own, _ = self._processes()
        deadline = time.monotonic() + 4
        while own and time.monotonic() < deadline:
            time.sleep(0.1)
            own, _ = self._processes()
        monitor_pid = self._monitor_pid()
        failures = []
        if monitor_pid:
            try:
                from process_cleanup import stop_process_tree
                stop_process_tree(psutil.Process(monitor_pid), timeout_s=2)
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                failures.append("captcha_monitor_stop_failed")
        if not own:
            return {"ok": not failures, "already_stopped": not failures,
                    **({"error": failures[0]} if failures else {})}
        session = self._active_session()
        # Stop the elevated worker first. If Windows denies access, keep the
        # launcher intact and report the actual failure.
        own.sort(key=lambda proc: proc.pid != (session or {}).get("pid"))
        for proc in own:
            try:
                from process_cleanup import stop_process_tree
                stop_process_tree(proc, timeout_s=2)
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                failures.append("captcha_stop_requires_admin")
            except psutil.TimeoutExpired:
                failures.append("captcha_stop_timeout")
        return {"ok": not failures, **({"error": failures[0]} if failures else {})}
