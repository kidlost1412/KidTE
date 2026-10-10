"""A fresh local backend must expose observer state without starting a solver."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
ENTRY = PROJECT / "backend" / "src" / "app_entry.py"


def test_captcha_api_in_fresh_backend(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    shutil.copytree(PROJECT / "plugins", tmp_path / "plugins",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = {**os.environ, "KID_TE_ROOT": str(tmp_path / "backend")}
    with (tmp_path / "stderr.txt").open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "--no-ui", "--port", str(port)],
            cwd=PROJECT, env=env, stdout=subprocess.DEVNULL, stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(80):
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(base + "/api/captcha", timeout=0.4) as response:
                        state = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            else:
                raise AssertionError("Kid TE did not start")
            assert proc.poll() is None, (tmp_path / "stderr.txt").read_text(errors="replace")
            assert state["configured"] is False
            assert state["running"] is False
            assert "events" in state
            with urllib.request.urlopen(base + "/api/dashboard", timeout=4) as response:
                dashboard = json.load(response)
            assert dashboard["captcha"]["configured"] is False
        finally:
            proc.terminate()
            proc.wait(timeout=10)
