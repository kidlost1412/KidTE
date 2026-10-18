import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import instance_lock
from instance_lock import InstanceAlreadyRunning, SingleInstanceLock


def test_second_instance_is_rejected_and_owner_releases(tmp_path):
    path = tmp_path / "runtime" / "live-re.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    with pytest.raises(InstanceAlreadyRunning, match="không tạo bản trùng"):
        SingleInstanceLock(path).acquire()
    first.release()
    assert not path.exists()


def test_stale_lock_from_dead_pid_is_replaced(tmp_path):
    path = tmp_path / "runtime" / "live-re.lock"
    path.parent.mkdir()
    path.write_text(json.dumps({"pid": 99999999, "token": "old"}), encoding="utf-8")
    lock = SingleInstanceLock(path)
    lock.acquire()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lock.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_exited_process_with_open_handle_does_not_keep_lock(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"],
                             creationflags=subprocess.CREATE_NO_WINDOW)
    child.wait(timeout=10)
    # Popen still holds a Windows handle to this terminated process.
    assert instance_lock._pid_is_alive(child.pid) is False


def test_fresh_corrupt_lock_is_not_taken_while_it_may_still_be_written(tmp_path):
    path = tmp_path / "runtime" / "live-re.lock"
    path.parent.mkdir()
    path.write_text("không phải json", encoding="utf-8")
    with pytest.raises(InstanceAlreadyRunning, match="tạo dở"):
        SingleInstanceLock(path).acquire()
    assert path.exists()


@pytest.mark.parametrize("content", [b"", b"{\"pid\": 12", b"\x00\x00\x00", b"[1, 2]"])
def test_old_empty_or_corrupt_lock_is_replaced_and_app_still_starts(tmp_path, content):
    """C4: mất điện giữa lúc tạo khoá để lại tệp rỗng/hỏng -> không được khoá app mãi."""
    path = tmp_path / "runtime" / "live-re.lock"
    path.parent.mkdir()
    path.write_bytes(content)
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))
    lock = SingleInstanceLock(path)
    lock.acquire()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert lock.stale_lock == {"pid": 0, "corrupt": "unreadable"}
    lock.release()
    assert not path.exists()


def test_lock_is_written_whole_and_leaves_no_temporary_files(tmp_path):
    path = tmp_path / "runtime" / "live-re.lock"
    lock = SingleInstanceLock(path)
    lock.acquire()
    assert json.loads(path.read_bytes())["token"] == lock.token
    assert [p.name for p in path.parent.iterdir()] == ["live-re.lock"]
    lock.release()


def test_legacy_lock_with_reused_pid_for_other_executable_is_replaced(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "live-re.lock"
    path.parent.mkdir()
    path.write_text(json.dumps({"pid": 304, "token": "old"}), encoding="utf-8")
    monkeypatch.setattr(instance_lock, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(
        instance_lock, "_process_identity",
        lambda pid: {"executable": "C:/tools/node.exe", "created_at": 1.0}
        if pid == 304 else {
            "executable": sys.executable, "created_at": 2.0,
        },
    )

    lock = SingleInstanceLock(path)
    lock.acquire()

    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lock.release()
