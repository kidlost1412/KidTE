"""Supervisor tối thiểu cho service plugin, không tự ý thao tác service thật.

Supervisor chỉ quản lý process được đăng ký qua ``ServiceSpec``. PID file là
bằng chứng khôi phục sau khi core mở lại, nhưng chỉ được nhận lại khi executable
khớp; PID bị tái sử dụng sẽ bị coi là stale, không được kill.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from instance_lock import _pid_is_alive
from process_cleanup import stop_process_tree


class SupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    command: tuple[str, ...]
    cwd: Path

    def __post_init__(self):
        if not self.service_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in self.service_id):
            raise SupervisorError("service_id chỉ dùng chữ thường, số và dấu gạch ngang")
        if not self.command:
            raise SupervisorError("Service thiếu command")


_BACKEND_SRC = Path(__file__).resolve().parent


def code_fingerprint(*roots: Path) -> str:
    """Hash nội dung mọi file .py dưới các thư mục gốc (plugin + backend/src).

    Ghi vào PID file lúc khởi chạy để core mở lại nhận ra service cũ đang chạy
    mã đã lỗi thời (nếu không, nó bị attach lại và mã mới không bao giờ chạy).
    """
    digest = hashlib.sha256()
    for root in roots:
        root = Path(root)
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


class ServiceSupervisor:
    def __init__(self, runtime_dir: Path, *, popen=subprocess.Popen,
                 clock=time.monotonic, pid_alive=_pid_is_alive,
                 image_for_pid=None, process_for_pid=None, max_restarts=3, restart_window_s=300,
                 fingerprint_roots=None):
        self.runtime_dir = Path(runtime_dir)
        self.popen = popen
        self.clock = clock
        self.pid_alive = pid_alive
        self.image_for_pid = image_for_pid or self._image_for_pid
        self.process_for_pid = process_for_pid or self._process_for_pid
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self._fingerprint_roots = fingerprint_roots
        self._processes = {}
        self._restart_times = {}

    def _pid_path(self, service_id: str) -> Path:
        return self.runtime_dir / "services" / f"{service_id}.pid.json"

    @staticmethod
    def _image_for_pid(pid: int) -> str:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=3, check=False,
            )
            row = result.stdout.strip().split("\n", 1)[0].strip()
            if row.startswith('"'):
                return row.split('","', 1)[0].strip('"')
        except (OSError, subprocess.SubprocessError):
            pass
        return ""

    def current_fingerprint(self, spec: ServiceSpec) -> str:
        roots = (self._fingerprint_roots if self._fingerprint_roots is not None
                 else (spec.cwd, _BACKEND_SRC))
        return code_fingerprint(*roots)

    def _read_record(self, spec: ServiceSpec) -> dict | None:
        try:
            value = json.loads(self._pid_path(spec.service_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_record(self, spec: ServiceSpec, pid: int) -> None:
        path = self._pid_path(spec.service_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1, "service_id": spec.service_id, "pid": pid,
            "executable": Path(spec.command[0]).name.casefold(),
            "command": list(spec.command), "started_monotonic": self.clock(),
            "code_fingerprint": self.current_fingerprint(spec),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _remove_record(self, spec: ServiceSpec) -> None:
        try:
            self._pid_path(spec.service_id).unlink()
        except FileNotFoundError:
            pass

    def attach(self, spec: ServiceSpec) -> dict:
        """Nhận lại service sống sau core restart, chỉ khi PID/image còn đúng."""
        record = self._read_record(spec)
        if not record:
            return {"state": "stopped", "reason": "no_pid_file"}
        pid = record.get("pid")
        expected = Path(spec.command[0]).name.casefold()
        if not isinstance(pid, int) or not self.pid_alive(pid):
            self._remove_record(spec)
            return {"state": "stopped", "reason": "stale_pid"}
        actual = self.image_for_pid(pid).casefold()
        if not actual or actual != expected or record.get("executable") != expected:
            self._remove_record(spec)
            return {"state": "stopped", "reason": "pid_identity_mismatch"}
        started = record.get("started_monotonic")
        uptime = (max(0.0, self.clock() - float(started))
                  if isinstance(started, (int, float)) else None)
        return {"state": "running", "pid": pid, "attached": True,
                "uptime_s": round(uptime, 1) if uptime is not None else None,
                "code_stale": record.get("code_fingerprint") != self.current_fingerprint(spec)}

    def start(self, spec: ServiceSpec, *, manual: bool = False) -> dict:
        """``manual``: người vận hành tự bấm Bật — không tính vào giới hạn chống
        vòng lặp crash (bản cũ khoá dịch vụ sau 3 lần bấm / đổi âm thanh trong 5 phút)."""
        current = self.attach(spec)
        if current["state"] == "running" and current.get("code_stale"):
            # Service do core trước khởi chạy còn chạy mã cũ: dừng cả cây tiến
            # trình (launcher venv + python con) rồi khởi chạy mới bằng mã hiện tại.
            stopped = self.stop(spec)
            if stopped.get("state") != "stopped":
                return {"state": "needs_attention", "reason": "stale_code_stop_failed"}
            current = {"state": "stopped"}
        if current["state"] == "running":
            return current | {"started": False}
        now = self.clock()
        times = [item for item in self._restart_times.get(spec.service_id, [])
                 if now - item <= self.restart_window_s]
        if manual:
            times = []
        if len(times) >= self.max_restarts:
            self._restart_times[spec.service_id] = times
            return {"state": "needs_attention", "reason": "restart_limit"}
        if times:
            delay = min(30.0, 2.0 ** (len(times) - 1))
            remaining = delay - (now - times[-1])
            if remaining > 0:
                return {"state": "backoff", "reason": "restart_backoff",
                        "retry_after_s": round(remaining, 3)}
        kwargs = {"cwd": str(spec.cwd), "shell": False}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                        | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process = self.popen(list(spec.command), **kwargs)
        except OSError as exc:
            self._restart_times[spec.service_id] = times + [now]
            return {"state": "needs_attention", "reason": "start_failed",
                    "error": type(exc).__name__}
        self._processes[spec.service_id] = process
        self._write_record(spec, int(process.pid))
        self._restart_times[spec.service_id] = times + [now]
        return {"state": "running", "pid": int(process.pid), "started": True}

    @staticmethod
    def _command_marker(spec: ServiceSpec) -> str:
        """Dấu hiệu nhận đúng tiến trình của service: file chạy cuối cùng (chay.py / .exe)."""
        return Path(spec.command[-1] if len(spec.command) > 1 else spec.command[0]).name.casefold()

    def _stop_attached(self, spec: ServiceSpec, pid: int, timeout_s: float) -> dict:
        """Dừng service mà core hiện tại KHÔNG tự khởi chạy (nhận lại từ PID file).

        Chỉ dừng khi dòng lệnh của PID đó chứa đúng file chạy của service — PID
        bị hệ điều hành cấp lại cho tiến trình khác thì không bao giờ bị động tới.
        Không có psutil thì giữ nguyên hành vi an toàn cũ (báo người xử lý).
        """
        process = self.process_for_pid(pid)
        if process is None:
            return {"state": "needs_attention", "reason": "unmanaged_running"}
        try:
            command_line = " ".join(process.cmdline()).casefold()
            if self._command_marker(spec) not in command_line:
                return {"state": "needs_attention", "reason": "pid_identity_mismatch"}
            stop_process_tree(process, timeout_s)
        except Exception as exc:          # psutil.TimeoutExpired / NoSuchProcess / AccessDenied
            name = type(exc).__name__
            if name == "TimeoutExpired":
                return {"state": "needs_attention", "reason": "stop_timeout"}
            if name != "NoSuchProcess":
                return {"state": "needs_attention", "reason": "unmanaged_running"}
        self._remove_record(spec)
        return {"state": "stopped", "reason": "graceful_attached"}

    @staticmethod
    def _process_for_pid(pid):
        try:
            import psutil
            return psutil.Process(pid)
        except Exception:
            return None

    def status(self, spec: ServiceSpec) -> dict:
        return self.attach(spec)

    def stop(self, spec: ServiceSpec, timeout_s: float = 10.0) -> dict:
        """Dừng mềm process do core hiện tại khởi chạy.

        Một PID chỉ được attach từ file sau core restart không có process handle
        đáng tin cậy, nên không được terminate theo PID; UI phải báo rõ để
        người vận hành xử lý thay vì có nguy cơ giết PID tái sử dụng.
        """
        process = self._processes.get(spec.service_id)
        if process is None:
            current = self.attach(spec)
            if current["state"] == "running":
                return self._stop_attached(spec, current["pid"], timeout_s)
            return {"state": "stopped", "reason": "not_running"}
        try:
            stop_process_tree(process, timeout_s)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutExpired":
                return {"state": "needs_attention", "reason": "stop_failed"}
            return {"state": "needs_attention", "reason": "stop_timeout"}
        self._processes.pop(spec.service_id, None)
        self._remove_record(spec)
        return {"state": "stopped", "reason": "graceful"}
