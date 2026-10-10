"""Independent source settings for each LIVE platform. Secrets stay local."""
from __future__ import annotations

import copy
import json
import math
import threading
import time
from pathlib import Path

PLATFORMS = ("tiktok", "shopee")
DEFAULTS = {
    "tiktok": {"enabled": True, "mode": "process", "process_name": "TikTok LIVE Studio.exe",
               "device_id": "", "offset_ms": 0},
    "shopee": {"enabled": True, "mode": "obs", "port": 4455, "password": "",
               "input_name": "", "bindings": {}, "offset_ms": 0},
}


class LiveSources:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self, public=False):
        with self.lock:
            data = copy.deepcopy(DEFAULTS)
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                for platform in PLATFORMS:
                    if isinstance(saved.get(platform), dict):
                        data[platform].update(saved[platform])
            except (OSError, ValueError):
                pass
            if public:
                data["shopee"]["password_configured"] = bool(data["shopee"].pop("password", ""))
            return data

    def save(self, payload):
        platform = payload.get("platform")
        if platform not in PLATFORMS:
            raise ValueError("platform_required")
        with self.lock:
            data = self.read()
            item = data[platform]
            patch = payload.get("settings")
            if not isinstance(patch, dict) or set(patch) - set(DEFAULTS[platform]):
                raise ValueError("invalid_source_settings")
            patch = copy.deepcopy(patch)
            if "enabled" in patch and not isinstance(patch["enabled"], bool):
                raise ValueError("enabled_must_be_bool")
            if "offset_ms" in patch:
                try:
                    value = float(patch["offset_ms"])
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("offset_out_of_range") from None
                if not math.isfinite(value) or abs(value) > 60000:
                    raise ValueError("offset_out_of_range")
                patch["offset_ms"] = value
            if platform == "shopee":
                if patch.get("mode", "obs") != "obs":
                    raise ValueError("shopee_requires_obs")
                if "port" in patch:
                    try:
                        patch["port"] = int(patch["port"])
                    except (TypeError, ValueError, OverflowError):
                        raise ValueError("invalid_obs_port") from None
                    if not 1024 <= patch["port"] <= 65535:
                        raise ValueError("invalid_obs_port")
                if "bindings" in patch:
                    if not isinstance(patch["bindings"], dict) or len(patch["bindings"]) > 100:
                        raise ValueError("invalid_obs_bindings")
                    for key, value in patch["bindings"].items():
                        if not key or not isinstance(value, str) or not value or len(key) > 1024:
                            raise ValueError("invalid_obs_binding")
            elif patch.get("mode", item["mode"]) not in ("process", "device"):
                raise ValueError("invalid_tiktok_source")
            for key in ("password", "input_name", "process_name", "device_id"):
                if key in patch and (not isinstance(patch[key], str) or len(patch[key]) > 1024 or
                                     any(c in patch[key] for c in "\r\n\0")):
                    raise ValueError("invalid_source_text")
            item.update(patch)
            if platform == "tiktok" and (not item["process_name"] or
                                         item["mode"] == "device" and not item["device_id"]):
                raise ValueError("source_required")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
            return self.read(public=True)

    def request_reidentify(self, platform):
        if platform not in PLATFORMS:
            raise ValueError('invalid_platform')
        with self.lock:
            data = self.read()
            token = time.time_ns()
            data[platform]['reidentify_token'] = token
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.path)
            return {'ok': True, 'platform': platform, 'request_id': str(token)}


def read_channel_health(root, platform):
    try:
        path = Path(root) / "runtime" / "services" / f"live-{platform}.health.json"
        if time.time() - path.stat().st_mtime > 15:
            return {"state": "waiting", "reason": "source_not_started"}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "waiting", "reason": "source_not_started"}
