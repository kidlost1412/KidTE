"""Stop an owned process and the descendants captured before it exits."""
from __future__ import annotations

import subprocess
import psutil


def stop_process_tree(process, timeout_s=3):
    children = []
    try:
        if isinstance(process, subprocess.Popen) and process.poll() is not None:
            # Its PID may already belong to a different process. Never inspect
            # or terminate descendants found through a reused PID.
            return
        root = psutil.Process(process.pid) if isinstance(process, subprocess.Popen) else process
        if callable(getattr(root, "children", None)):
            children = list(root.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
        pass
    targets = list(reversed(children)) + [process]
    failures = []
    for target in targets:
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            failures.append(exc)
    for target in targets:
        try:
            target.wait(timeout=max(0, float(timeout_s)))
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            if not callable(getattr(target, "kill", None)):
                failures.append(subprocess.TimeoutExpired(str(target.pid), timeout_s))
                continue
            try:
                target.kill()
                target.wait(timeout=max(0, float(timeout_s)))
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, subprocess.TimeoutExpired) as exc:
                failures.append(exc)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            failures.append(exc)
    if failures:
        raise failures[0]
