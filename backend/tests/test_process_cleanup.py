import subprocess
import sys
from unittest import mock

import psutil

from process_cleanup import stop_process_tree


def test_stop_captures_children_before_parent_and_escalates_timeout():
    events = []
    child = mock.Mock(pid=2)
    child.terminate.side_effect = lambda: events.append("child")
    child.wait.side_effect = [psutil.TimeoutExpired(0), 0]
    parent = mock.Mock(pid=1)
    parent.children.return_value = [child]
    parent.terminate.side_effect = lambda: events.append("parent")
    stop_process_tree(parent, 0)
    assert events == ["child", "parent"]
    child.kill.assert_called_once()
    parent.kill.assert_not_called()


def test_real_owned_parent_and_child_both_exit():
    script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline())
    child = psutil.Process(child_pid)
    try:
        stop_process_tree(parent, 2)
        assert parent.poll() is not None
        assert not child.is_running()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child.is_running():
            child.kill()
            child.wait(timeout=2)
        parent.stdout.close()


def test_exited_owned_process_does_not_capture_a_reused_pid():
    parent = subprocess.Popen([sys.executable,'-c','pass'])
    parent.wait(timeout=5)
    with mock.patch('process_cleanup.psutil.Process') as lookup:
        stop_process_tree(parent)
        lookup.assert_not_called()
