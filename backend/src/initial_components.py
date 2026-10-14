"""First-install feature selection; requests come only from the local installer."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from update_catalog import _version_key

MARKER = "initial-components.json"
FEATURES = {"media-tools", "captcha-solver", "video-sync", "video-analysis-cpu"}


def read_request(app_root):
    path = Path(app_root) / MARKER
    try:
        if path.stat().st_size > 16384:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema_version") == 1 else None


def detect_gpu():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return {"eligible": False, "reason": "driver_unavailable"}
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 3 or not re.fullmatch(r"\d+\.\d+", parts[1]) or not re.fullmatch(r"\d+\.\d+", parts[2]):
                continue
            driver = tuple(map(int, parts[1].split(".")))
            # CUDA 12 minor-version compatibility: Windows driver >=527.41.
            if driver >= (527, 41) and float(parts[2]) >= 5:
                return {"eligible": True, "name": parts[0], "reason": "nvidia_detected"}
        return {"eligible": False, "reason": "driver_or_gpu_unsupported"}
    except (OSError, subprocess.SubprocessError):
        return {"eligible": False, "reason": "nvidia_not_detected"}


def selection(catalog, installed, gpu):
    chosen = {}
    for p in catalog["components"]:
        component = p["id"]
        if component not in FEATURES | {"gpu-cuda12", "app", "runtime", "extensions", "core"}:
            continue
        current = installed.get(component)
        if current and _version_key(p["version"]) > _version_key(current):
            chosen[component] = p["version"]
        elif not current and (component in FEATURES or component == "gpu-cuda12" and gpu.get("eligible")):
            chosen[component] = p["version"]
    return chosen
