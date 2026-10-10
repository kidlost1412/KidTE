"""Khóa một phiên Kid TE, không giết tiến trình lạ để giành cổng."""
from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class InstanceLockError(RuntimeError):
    pass


class InstanceAlreadyRunning(InstanceLockError):
    """Một phiên còn sống hoặc lock không an toàn để cướp."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # `os.kill(pid, 0)` không có semantics probe như POSIX trên Windows;
        # tùy runtime nó có thể làm chết chính tiến trình test. OpenProcess với
        # quyền truy vấn giới hạn là kiểm tra chỉ-đọc đúng cho Windows.
        import ctypes

        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information | synchronize, False, pid,
        )
        if not handle:
            # Access denied is not evidence that the process exited.
            return ctypes.windll.kernel32.GetLastError() == 5
        try:
            # OpenProcess also succeeds for an exited process when another
            # program still holds a Windows process handle. The handle is then
            # signaled, whereas a live process returns WAIT_TIMEOUT (258).
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 258
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Không có quyền kiểm tra tiến trình khác không phải là bằng chứng nó
        # đã chết; ưu tiên an toàn, không chiếm lock đó.
        return True
    return True


def _process_identity(pid: int) -> dict | None:
    """Trả về danh tính đủ để phát hiện Windows tái sử dụng PID."""
    try:
        import psutil
        process = psutil.Process(pid)
        return {
            "executable": os.path.normcase(os.path.abspath(process.exe())),
            "created_at": round(float(process.create_time()), 3),
        }
    except Exception:
        return None


def _same_process(existing: dict, pid: int) -> bool:
    current = _process_identity(pid)
    if current is None:
        # Không đọc được danh tính không phải là bằng chứng an toàn để cướp lock.
        return True
    recorded = existing.get("process_identity")
    if isinstance(recorded, dict):
        try:
            return (
                current["executable"] == os.path.normcase(os.path.abspath(recorded["executable"]))
                and abs(current["created_at"] - float(recorded["created_at"])) < 1.0
            )
        except (KeyError, TypeError, ValueError):
            return True
    # Lock schema cũ chưa có thời điểm tạo tiến trình. Chỉ coi là cùng app nếu
    # executable có cùng tên; như vậy PID bị tái sử dụng cho node.exe không
    # thể giữ Kid TE bị khóa vĩnh viễn.
    return Path(current["executable"]).name.casefold() == Path(sys.executable).name.casefold()


@dataclass
class SingleInstanceLock:
    path: Path
    token: str | None = None
    # Lock cua lan chay TRUOC con sot lai, neu lan nay phai don no di.
    #
    # Mot tien trinh bi kill cung (hoac mat dien) khong chay duoc khoi `finally`
    # nen khong ghi duoc dong log nao — lan chay do bien mat khong dau vet.
    # File lock sot lai la BANG CHUNG duy nhat con lai; giu no o day de
    # app_entry ghi vao log ngay khi khoi dong lai.
    stale_lock: dict | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(24)
        payload = {
            "schema_version": 1,
            "pid": os.getpid(),
            "started_at": _now(),
            "token": self.token,
            "process_identity": _process_identity(os.getpid()),
        }
        try:
            descriptor = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            existing = self._read_existing()
            pid = existing.get("pid") if isinstance(existing, dict) else 0
            if isinstance(pid, int) and _pid_is_alive(pid) and _same_process(existing, pid):
                raise InstanceAlreadyRunning(
                    f"Kid TE đang chạy (PID {pid}); không tạo bản trùng."
                ) from None
            # Chỉ xóa file lock thuộc đúng thư mục runtime sau khi PID đã chết.
            # Không kill theo PID và không đụng cổng do tiến trình lạ giữ.
            truoc_do = existing if isinstance(existing, dict) else None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.acquire()
            # Giữ lại sau khi acquire() đệ quy, vì lần gọi đó đặt lại thuộc tính.
            self.stale_lock = truoc_do
            return
        try:
            os.write(descriptor, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)

    def _read_existing(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # File hỏng không chứng minh có thể khởi động thêm một bản. Cần
            # người vận hành dọn sau khi chắc chắn không có phiên đang chạy.
            raise InstanceAlreadyRunning(
                f"Không đọc được lock hiện có: {self.path}. Không tự xóa an toàn."
            ) from None
        if not isinstance(value, dict):
            raise InstanceAlreadyRunning(f"Lock hiện có không hợp lệ: {self.path}")
        return value

    def release(self) -> None:
        if not self.token:
            return
        try:
            existing = self._read_existing()
        except InstanceAlreadyRunning:
            return
        if existing.get("token") == self.token:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.token = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()
