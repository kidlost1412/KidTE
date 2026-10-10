"""Read-only OBS WebSocket 5 client; no stream, scene or media writes."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import math
import json
import os
import sqlite3
import uuid


class ObsClient:
    def __init__(self, port=4455, password="", timeout=3):
        import websocket
        self.socket = websocket.create_connection(f"ws://127.0.0.1:{int(port)}", timeout=timeout,
                                                   http_proxy_host=None, http_no_proxy=["127.0.0.1"])
        try:
            hello = json.loads(self.socket.recv())
            if hello.get("op") != 0:
                raise ValueError("obs_invalid_hello")
            identify = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hello["d"].get("authentication")
            if auth:
                secret = base64.b64encode(hashlib.sha256((password + auth["salt"]).encode()).digest()).decode()
                identify["authentication"] = base64.b64encode(hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
            self.socket.send(json.dumps({"op": 1, "d": identify}))
            if json.loads(self.socket.recv()).get("op") != 2:
                raise ValueError("obs_auth_failed")
        except Exception:
            self.close()
            raise

    def close(self):
        self.socket.close()

    def request(self, kind, **data):
        if not kind.startswith("Get"):
            raise ValueError("obs_read_only")
        request_id = uuid.uuid4().hex
        self.socket.send(json.dumps({"op": 6, "d": {"requestType": kind, "requestId": request_id,
                                                     "requestData": data}}))
        for _ in range(100):
            message = json.loads(self.socket.recv())
            response = message.get("d", {})
            if message.get("op") == 7 and response.get("requestId") == request_id:
                if not response.get("requestStatus", {}).get("result"):
                    raise ValueError(f"obs_request_failed:{kind}")
                return response.get("responseData", {})
        raise ValueError("obs_no_response")

    def media_inputs(self):
        scene = self.request("GetCurrentProgramScene")["currentProgramSceneName"]
        visible = set()
        visited = set()
        def walk(name, group=False, depth=0):
            if depth > 8 or (name, group) in visited:
                return
            visited.add((name, group))
            items = self.request("GetGroupSceneItemList" if group else "GetSceneItemList", sceneName=name)
            for item in items.get("sceneItems", []):
                if not item.get("sceneItemEnabled"):
                    continue
                if item.get("isGroup"):
                    walk(item["sourceName"], True, depth + 1)
                elif item.get("sourceType") == "OBS_SOURCE_TYPE_SCENE":
                    walk(item["sourceName"], False, depth + 1)
                elif item.get("inputKind") == "ffmpeg_source":
                    visible.add(item["sourceName"])
        walk(scene)
        result = []
        for name in sorted(visible):
            settings = self.request("GetInputSettings", inputName=name).get("inputSettings", {})
            media = self.request("GetMediaInputStatus", inputName=name)
            result.append({"input_name": name, "media_path": settings.get("local_file", ""),
                           "local_file": settings.get("is_local_file", True), **media})
        return result


def resolve_obs_video(item, settings, timelines):
    path = str(item.get("media_path") or "")
    if not path or not item.get("local_file"):
        return None
    # A binding belongs to the exact file, not to a reusable OBS input name.
    normalized = path.replace("\\", "/").casefold()
    bindings = {key.replace("\\", "/").casefold(): value for key, value in settings.get("bindings", {}).items()}
    bound = bindings.get(normalized)
    if bound:
        return bound if bound in timelines else None
    verified = item.get("verified_video_id")
    return verified if verified in timelines else None


def obs_measurement(client, settings, timelines):
    items = client.media_inputs()
    if not client.request("GetStreamStatus").get("outputActive"):
        return None, "obs_not_streaming", items
    selected = settings.get("input_name", "")
    candidates = [item for item in items if (not selected or item["input_name"] == selected)
                  and item.get("mediaState") == "OBS_MEDIA_STATE_PLAYING"]
    if len(candidates) != 1:
        return None, "obs_select_one_media" if len(candidates) > 1 else "obs_media_not_playing", items
    item = candidates[0]
    video_id = resolve_obs_video(item, settings, timelines)
    if not video_id:
        return None, "obs_video_not_linked", items
    timeline = timelines[video_id]
    try:
        duration = float(item.get("mediaDuration", 0)) / 1000
        expected = float(timeline.get("cycle_ms", 0)) / 1000
        position = (float(item["mediaCursor"]) + float(settings.get("offset_ms", 0))) / 1000
    except (ValueError, TypeError, OverflowError, KeyError):
        return None, 'obs_position_invalid', items
    if not all(math.isfinite(value) for value in (duration, expected, position)):
        return None, 'obs_position_invalid', items
    if duration <= 0 or abs(duration - expected) > max(2, expected * 0.005):
        return None, "obs_video_duration_mismatch", items
    return {"video_id": video_id, "position_s": position % duration,
            "video_duration_s": duration, "confidence": 1, "margin": 1,
            "source": "obs_media", "input_name": item["input_name"]}, "", items


def source_identity(path, library_revision=0):
    """Cache key for a media file: (abs_path, size, mtime_ns, library_revision)."""
    stat = os.stat(path)
    return (os.path.abspath(str(path)), stat.st_size, stat.st_mtime_ns, int(library_revision))


class MediaBindingStore:
    """Persists library_revision and per-file manual_binding_sku in SQLite."""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS media_library_meta(id INTEGER PRIMARY KEY CHECK(id=1), library_revision INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO media_library_meta(id, library_revision) VALUES(1, 0)")
            db.execute("CREATE TABLE IF NOT EXISTS media_source_bindings(abs_path TEXT PRIMARY KEY, manual_binding_sku TEXT)")

    @contextlib.contextmanager
    def _connect(self):
        # Đóng kết nối sau mỗi lần dùng: Windows giữ khoá file nếu để GC tự dọn.
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def library_revision(self):
        with self._connect() as db:
            return db.execute("SELECT library_revision FROM media_library_meta WHERE id=1").fetchone()[0]

    def bump_library_revision(self):
        with self._connect() as db:
            db.execute("UPDATE media_library_meta SET library_revision=library_revision+1 WHERE id=1")
        return self.library_revision()

    def set_manual_binding(self, path, sku):
        key = os.path.abspath(str(path))
        with self._connect() as db:
            db.execute("INSERT INTO media_source_bindings(abs_path, manual_binding_sku) VALUES(?,?) "
                       "ON CONFLICT(abs_path) DO UPDATE SET manual_binding_sku=excluded.manual_binding_sku",
                       (key, sku or None))
        self.bump_library_revision()

    def manual_binding(self, path):
        with self._connect() as db:
            row = db.execute("SELECT manual_binding_sku FROM media_source_bindings WHERE abs_path=?",
                             (os.path.abspath(str(path)),)).fetchone()
        return row[0] if row else None
