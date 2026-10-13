"""Two independent LIVE trackers. No source falls back to the other platform."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "src"))
import numpy as np
from audio_capture import RollingCapture
from config import ROOT, CHAT_HOST, CHAT_PORT
from live_sources import LiveSources
import live_state_store
from live_session import VideoLock
from obs_source import ObsClient, obs_measurement
from process_audio import ProcessCapture, studio_process
from video_library import list_videos, load_references
from video_sync import best_match, write_health
from media_assets import _ffmpeg
from video_fingerprint import locate, spectral_features, TARGET_RATE


def verify_obs_file(path, references):
    """Verify two file excerpts against library fingerprints, independent of its name."""
    candidates = None
    for at_s in (30, 180):
        result = subprocess.run([_ffmpeg("ffmpeg"), "-v", "error", "-ss", str(at_s),
            "-i", str(path), "-t", "8", "-vn", "-ac", "1", "-ar", str(TARGET_RATE),
            "-f", "f32le", "pipe:1"], capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode or not result.stdout:
            return None
        samples = np.frombuffer(result.stdout, dtype="<f4")
        features = spectral_features(samples, TARGET_RATE)
        matched = set()
        for reference in references:
            if "error" in reference:
                continue
            found = locate(reference["features"], features)
            if found and found[2] >= 0.06 and abs(found[0] - at_s) < 1.5:
                matched.add(reference["video_id"])
        candidates = matched if candidates is None else candidates & matched
        if not candidates:
            return None
    return next(iter(candidates)) if len(candidates) == 1 else None


class VerifiedObs:
    def __init__(self, client):
        self.client = client
        self.cache = {}
        self.errors = {}
        self.persisted = None      # Shopee persisted binding entry (restart reuse), None = disabled
        self.room_id = ""
        self.last_bound = None

    def _persist(self, item, key, video_id):
        try:
            marker = (item["input_name"], key, video_id)
            now = time.time()
            if item.get("mediaState") != "OBS_MEDIA_STATE_PLAYING":
                return  # only refresh freshness while actually seen playing
            if marker == self.last_bound and now - getattr(self, "last_write", 0) < 60:
                return
            live_state_store.save_platform("shopee", {
                "room_id": self.room_id,
                "obs_binding": {"input_name": item["input_name"], "identity": list(key),
                                "verified_video_id": video_id,
                                "last_cursor": item.get("mediaCursor"), "measured_at": now}})
            self.last_bound = marker
            self.last_write = now
        except Exception:
            pass

    def close(self):
        self.client.close()

    def request(self, *args, **kwargs):
        return self.client.request(*args, **kwargs)

    def media_inputs(self):
        items = self.client.media_inputs()
        references = None
        for item in items:
            path = Path(item.get("media_path", ""))
            if not item.get("local_file") or not path.is_file():
                continue
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns, getattr(self, 'library_revision', 0))
            cached = self.cache.get(key)
            if not cached and self.persisted is not None:
                try:
                    saved = live_state_store.fresh_obs_binding(self.persisted, item["input_name"], key)
                except Exception:
                    saved = None
                if saved:  # same file still in OBS, <10 min: skip fingerprinting; cursor stays live
                    cached = (saved["verified_video_id"], time.time())
                    self.cache[key] = cached
                    self.last_bound = (item["input_name"], key, cached[0])
            if not cached or (not cached[0] and time.time() - cached[1] > 30):
                references = references if references is not None else load_references()
                try:
                    cached = (verify_obs_file(path, references), time.time())
                    self.errors.pop(key, None)
                except Exception as exc:
                    # ffmpeg thiếu/hỏng, quá thời gian... : giữ kết nối OBS, báo đúng lý do
                    # và thử lại sau 30 giây thay vì ngắt-nối lại mỗi 2 giây.
                    cached = (None, time.time())
                    self.errors[key] = f"{type(exc).__name__}: {exc}"[:200]
                self.cache[key] = cached
                item.update(self.client.request("GetMediaInputStatus", inputName=item["input_name"]))
                if len(self.cache) > 20:
                    self.cache = {key: cached}
                    self.errors = {k: v for k, v in self.errors.items() if k == key}
            item["verified_video_id"] = cached[0]
            if not cached[0] and key in self.errors:
                item["verify_error"] = self.errors[key]
            if cached[0]:
                self._persist(item, key, cached[0])
        return items


def publish(platform, measurement=None):
    payload = {"platform": platform, **(measurement or {"available": False})}
    request = urllib.request.Request(f"http://{CHAT_HOST}:{CHAT_PORT}/api/video/position",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                                                   "Origin": f"http://{CHAT_HOST}:{CHAT_PORT}"})
    with urllib.request.urlopen(request, timeout=3) as response:
        body = json.loads(response.read())
        if not body.get("ok"):
            raise RuntimeError("position_not_accepted")


def health(platform, state, reason="", **data):
    write_health(ROOT / "runtime" / "services" / f"live-{platform}.health.json",
                 state=state, reason=reason, platform=platform, **data)


_BINDINGS = None


def current_library_revision():
    """C04: library_revision persisted by the backend (cheap SQLite read; 0 when unavailable)."""
    global _BINDINGS
    try:
        if _BINDINGS is None:
            from config import MEDIA_BINDINGS_DB
            from obs_source import MediaBindingStore
            _BINDINGS = MediaBindingStore(MEDIA_BINDINGS_DB)
        return int(_BINDINGS.library_revision())
    except Exception:
        return 0


def obs_loop(store):
    client = None
    connection = None
    while True:
        try:
            settings = store.read()["shopee"]
            if not settings["enabled"]:
                if client:
                    client.close()
                    client = None
                publish("shopee")
                health("shopee", "disabled")
                time.sleep(2)
                continue
            desired = (settings["port"], settings["password"])
            if not client or desired != connection:
                if client:
                    client.close()
                client = VerifiedObs(ObsClient(*desired))
                client.persisted = _safe_state("shopee")
                client.room_id = str(settings.get("room_id", "") or "")
                connection = desired
            client.library_revision = current_library_revision()
            timelines = {row["video_id"]: row["timeline"] for row in list_videos(False) if not row["errors"]}
            measured, reason, items = obs_measurement(client, settings, timelines)
            publish("shopee", measured)
            health("shopee", "tracking" if measured else "waiting", reason, inputs=items, detail="",
                   video_id=(measured or {}).get("video_id", ""), position_s=(measured or {}).get("position_s"),
                   device="OBS · " + (measured or {}).get("input_name", settings.get("input_name", "")),
                   source="obs_media")
        except Exception as exc:
            if client:
                client.close()
                client = None
            # Do not expose websocket authentication frames or passwords in health/logs.
            health("shopee", "waiting", "obs_connection_failed", detail=type(exc).__name__)
            try:
                publish("shopee")
            except Exception:
                pass
        time.sleep(2)


def consistent(previous, video_id, position, now, duration):
    if not previous or previous[0] != video_id:
        return False
    delta = (position - previous[1] - (now - previous[2])) % duration
    return min(delta, duration - delta) < 2.5


def _library_revision(references):
    import hashlib
    rows = sorted((str(r.get("video_id")), r.get("duration_s")) for r in references)
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()[:16]


def _safe_state(platform):
    try:
        return live_state_store.get_platform(platform)
    except Exception:
        return {}


def _restore_lock(video_lock, persisted, references):
    """Recent lock with the same library becomes 'uncertain' and is re-verified, never trusted."""
    try:
        saved = live_state_store.fresh_lock(persisted, _library_revision(references))
        if saved:
            ref = next((r for r in references if r.get("video_id") == saved["video_id"]), None)
            return video_lock.restore(saved, ref)
    except Exception:
        pass
    return False


def _persist_lock(video_lock, references, token, settings, last_saved):
    """Write only when the lock state changes; failures never break tracking."""
    try:
        locked = video_lock.state == "locked" and video_lock.reference is not None
        marker = (locked, video_lock.lock_id, video_lock.revision, token, bool(settings.get("listen_manual")))
        if marker == last_saved:
            return last_saved
        entry = {"room_id": settings.get("room_id", ""), "locked": locked,
                 "locked_at": time.time() if locked else 0,
                 "listen_manual": bool(settings.get("listen_manual")),
                 "reidentify_token": token}
        if locked:
            entry["lock"] = video_lock.export(_library_revision(references))
        elif video_lock.reference is not None:
            # Keep the previous recent lock while merely uncertain (e.g. restored, awaiting verify).
            old = _safe_state("tiktok")
            if old.get("lock") and old.get("locked"):
                entry.update(locked=True, locked_at=old.get("locked_at", 0), lock=old["lock"])
        live_state_store.save_platform("tiktok", entry)
        return marker
    except Exception:
        return last_saved


def tiktok_loop(store):
    capture = None
    video_lock = VideoLock()
    last_tick = time.monotonic()
    previous = None
    scope = None
    references = []
    loaded_at = 0
    persisted = _safe_state("tiktok")
    reidentify_token = persisted.get("reidentify_token")
    restore_tried = False
    last_saved = None
    while True:
        try:
            settings = store.read()["tiktok"]
            token = settings.get('reidentify_token')
            if token != reidentify_token:
                video_lock.reset()
                loaded_at = 0
                reidentify_token = token
                restore_tried = True
                persisted = {}
            if not restore_tried and video_lock.reference is None and references:
                restore_tried = True
                _restore_lock(video_lock, persisted, references)
            last_saved = _persist_lock(video_lock, references, token, settings, last_saved)
            process = studio_process(settings["process_name"]) if settings["mode"] == "process" else None
            identity = (settings["mode"], settings["device_id"], (process or {}).get("pid"),
                        (process or {}).get("create_time"), settings["enabled"], settings["offset_ms"])
            now = time.monotonic()
            if now - last_tick > 15:
                video_lock.uncertain()
            last_tick = now
            if identity != scope:
                if capture:
                    capture.close() if hasattr(capture, "close") else capture.__exit__(None, None, None)
                capture = None
                previous = None
                # A replaced process cannot silently choose a different video.
                video_lock.uncertain()
                scope = identity
                publish("tiktok")
            if not settings["enabled"] or settings["mode"] == "process" and not process:
                health("tiktok", "disabled" if not settings["enabled"] else "waiting",
                       "" if not settings["enabled"] else "tiktok_studio_not_running", video_id="", source=settings["mode"])
                time.sleep(2)
                continue
            if video_lock.reference is not None and time.monotonic() < video_lock.next_check:
                if capture:
                    capture.close() if hasattr(capture, 'close') else capture.__exit__(None, None, None)
                    capture = None
                publish("tiktok", {**video_lock.snapshot(), "margin": .06,
                    "source": "tiktok_process_audio" if settings["mode"] == "process" else "tiktok_dedicated_audio"})
                health("tiktok", video_lock.state, video_id=video_lock.reference['video_id'],
                       source=settings['mode'], device=settings['device_id'])
                time.sleep(3)
                continue
            if not capture:
                capture = (ProcessCapture(process["pid"], 8) if settings["mode"] == "process"
                           else RollingCapture(settings["device_id"], 8))
                capture.__enter__()
            if video_lock.reference is None and time.monotonic() - loaded_at > 30:
                references = [item for item in load_references() if "error" not in item]
                loaded_at = time.monotonic()
            if capture.error:
                raise RuntimeError(capture.error)
            block = capture.latest()
            if block is None:
                health("tiktok", "watching", "collecting_audio", source=settings["mode"], device=capture.name)
                time.sleep(2)
                continue
            if not references or np.sqrt(np.mean(block * block)) < 0.000316:
                previous = None
                video_lock.uncertain()
                publish("tiktok")
                health("tiktok", "waiting", "audio_silent" if references else "no_fingerprint", video_id="",
                       source=settings["mode"], device=capture.name)
                time.sleep(3)
                continue
            # Đã khoá: chỉ kiểm video đang khoá. Chưa chắc chắn: so cả thư viện để nhận ra video mới.
            if video_lock.reference is None:
                candidates = references
            elif video_lock.state == 'locked' or not references:
                candidates = [video_lock.reference]
            else:
                candidates = references
            found = best_match(candidates, block, capture.rate)
            if not found or found[3] < 0.06:
                previous = None
                video_lock.uncertain()
                publish("tiktok")
                health("tiktok", "waiting", "no_fingerprint_match", video_id="", source=settings["mode"], device=capture.name)
                time.sleep(3)
                continue
            reference, start, score, margin = found
            duration = reference["duration_s"]
            position = (start + 8 + settings["offset_ms"] / 1000) % duration
            now = time.monotonic()
            agreed = video_lock.observe(reference, position, margin)
            previous = (reference["video_id"], position, now)
            device_name = capture.name
            if agreed:
                publish("tiktok", {**video_lock.snapshot(),
                    "confidence": score, "margin": margin,
                    "source": "tiktok_process_audio" if settings["mode"] == "process" else "tiktok_dedicated_audio"})
                capture.close() if hasattr(capture, "close") else capture.__exit__(None, None, None)
                capture = None
            else:
                publish("tiktok", video_lock.snapshot() if video_lock.reference else None)
            health("tiktok", "tracking" if agreed else "watching", "" if agreed else "confirming_match",
                   video_id=reference["video_id"], position_s=position, margin=margin,
                   source=settings["mode"], device=device_name)
        except Exception as exc:
            video_lock.uncertain()
            health("tiktok", "error", "audio_device_error", detail=str(exc)[:160], video_id="")
            if capture:
                try:
                    capture.close() if hasattr(capture, 'close') else capture.__exit__(None, None, None)
                except Exception:
                    pass
                capture = None
            try:
                publish("tiktok")
            except Exception:
                pass
        time.sleep(3)


def main():
    store = LiveSources(ROOT / "data" / "machine" / "live_sources.json")
    threads = [threading.Thread(target=worker, args=(store,), name=name, daemon=True)
               for name, worker in (("shopee-obs", obs_loop), ("tiktok-studio", tiktok_loop))]
    for thread in threads:
        thread.start()
    while all(thread.is_alive() for thread in threads):
        time.sleep(1)
    raise RuntimeError("live_tracker_thread_stopped")
