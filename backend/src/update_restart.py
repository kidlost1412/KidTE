"""Restart Kid TE through KidTELauncher.exe so a prepared update takes effect.

The launcher keeps the core inside a kill-on-close Windows job, so a helper
started directly from the core would be closed with it. The helper is therefore
created through WMI (outside the job). It waits for the core and its launcher
to exit, then starts a fresh launcher, which activates the pending generation.
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

LAUNCHER_NAME = "KidTELauncher.exe"


def _quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def helper_script(launcher: Path, pids) -> str:
    waits = "".join(f"try {{ Wait-Process -Id {int(p)} -Timeout 90 -ErrorAction Stop }} catch {{}}; "
                    for p in pids if p and int(p) > 0)
    return (f"{waits}Start-Sleep -Seconds 1; "
            f"Start-Process -FilePath {_quote(launcher)} -WorkingDirectory {_quote(launcher.parent)}")


def schedule_restart(app_root, *, pids=None, runner=subprocess.run) -> dict:
    """Start the detached helper. Raises ValueError('launcher_missing') if absent."""
    launcher = Path(app_root) / LAUNCHER_NAME
    if not launcher.is_file():
        raise ValueError("launcher_missing")
    pids = list(pids) if pids is not None else [os.getpid(), os.getppid()]
    encoded = base64.b64encode(helper_script(launcher, pids).encode("utf-16-le")).decode("ascii")
    inner = f"powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    outer = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
             f"-Arguments @{{CommandLine={_quote(inner)}}}; if ($r.ReturnValue -ne 0) {{ exit 3 }}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", outer],
                    timeout=20, creationflags=flags, capture_output=True)
    if getattr(result, "returncode", 1) != 0:
        raise ValueError("restart_helper_failed")
    return {"ok": True, "pids": pids}
