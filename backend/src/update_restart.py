"""Restart Kid TE through its launcher so a prepared update takes effect.

No helper process, WMI or job-escape trick: the launcher already supervises the
core. The core asks for a restart by

1. writing ``restart-request.json`` into the App folder (carrying the launch nonce
   the launcher put in the core's environment), then
2. exiting with ``RESTART_EXIT_CODE``.

The launcher accepts the request only when the exit code and the marker nonce both
match the core it started, then immediately re-runs its normal selection: the
pending generation is activated with the usual readiness handshake / rollback and
the core is started again.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

RESTART_EXIT_CODE = 75           # EX_TEMPFAIL: "try again" - dedicated to launcher restarts
ALREADY_RUNNING_EXIT_CODE = 4    # another core already owns this data folder
MARKER_NAME = "restart-request.json"
RESTART_CAP = "restart-exit"     # advertised in KIDTE_LAUNCHER_CAPS by launchers that honour it


def marker_path(app_root) -> Path:
    return Path(app_root) / MARKER_NAME


def write_marker(app_root, nonce: str) -> None:
    """Record a restart request for the launcher (atomic replace)."""
    path = marker_path(app_root)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    body = {"schema_version": 1, "nonce": nonce, "pid": os.getpid(), "requested_at": time.time()}
    tmp.write_text(json.dumps(body), encoding="utf-8")
    os.replace(tmp, path)


def consume_marker(app_root, nonce: str, *, max_age: float = 600.0) -> bool:
    """True when a fresh marker for ``nonce`` exists; the marker is always removed."""
    path = marker_path(app_root)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        body = None
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(body, dict) or not nonce or body.get("nonce") != nonce:
        return False
    try:
        age = time.time() - float(body.get("requested_at"))
    except (TypeError, ValueError):
        return False
    return -5.0 <= age <= max_age


def launcher_supports_restart(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return (RESTART_CAP in (environ.get("KIDTE_LAUNCHER_CAPS", "") or "").split()
            and bool(environ.get("KIDTE_LAUNCH_NONCE", "")))


def evaluate_restart(*, has_pending: bool, live: bool, payload: dict, supported: bool) -> dict:
    """Decide what POST /api/updates/restart does. Returns {status, body, go}."""
    if not supported:
        return {"status": 409, "go": False,
                "body": {"ok": False, "error": "restart_unsupported"}}
    if not has_pending and payload.get("force") is not True:
        return {"status": 409, "go": False,
                "body": {"ok": False, "error": "update_nothing_to_apply"}}
    if live and payload.get("confirm_live") is not True:
        return {"status": 409, "go": False,
                "body": {"ok": False, "error": "live_confirm_required",
                         "needs_confirm": True, "live": True}}
    return {"status": 200, "go": True,
            "body": {"ok": True, "restarting": True, "live": bool(live)}}
