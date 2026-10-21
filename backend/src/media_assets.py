"""Tài nguyên hình ảnh/âm thanh phục vụ giao diện: ảnh sản phẩm, poster,
dải khung hình (filmstrip) và sóng âm của video.

Mọi thứ ở đây chỉ là BẢN DẪN XUẤT có thể xoá và tạo lại bất cứ lúc nào:
nằm trong ``data/cache`` và không bao giờ là nguồn sự thật của nghiệp vụ.

- Ảnh sản phẩm: link ảnh TikTok là URL có chữ ký, hết hạn sau vài ngày; ảnh
  website công ty có thể đổi. Tải MỘT lần về máy, chuẩn hoá thành JPEG nhỏ,
  từ đó giao diện chạy được cả khi mất mạng.
- Video: file gốc 3-4 GB, dọc 1080x2160. Người dựng cần nhìn thấy hình ở dọc
  timeline và sóng âm để biết đoạn nào có người nói. Tạo bằng ffmpeg ở luồng
  nền, một video tại một thời điểm, có trạng thái để giao diện hiển thị tiến độ.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

CACHE_VERSION = 2
PRODUCT_THUMB_MAX = 320
FILMSTRIP_MAX_FRAMES = 180
FILMSTRIP_MIN_INTERVAL_S = 5.0
FILMSTRIP_FRAME_H = 160
FILMSTRIP_COLS = 20
WAVEFORM_RATE = 2000
WAVEFORM_BUCKET_S = 0.25
POSTER_H = 480
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        temp = Path(handle.name)
        handle.write(data)
    try:
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError as exc:
                # Windows scanners can briefly hold the destination open.
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32) or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def _atomic_json(path: Path, value):
    _atomic_bytes(path, json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _ffmpeg(name):
    from config import find_tool
    found = find_tool(name)
    if not found:
        raise RuntimeError(f"missing_{name}")
    return found


def media_key(path: Path, library_revision: int = 0) -> str:
    """Khoá bộ nhớ đệm theo file vật lý (đường dẫn + cỡ + mtime_ns + library_revision)."""
    stat = Path(path).stat()
    raw = f"v{CACHE_VERSION}|{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    if library_revision:
        raw += f"|r{int(library_revision)}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def probe(path: Path) -> dict:
    command = [_ffmpeg("ffprobe"), "-v", "error", "-select_streams", "v:0",
               "-show_entries", "format=duration:stream=width,height,avg_frame_rate",
               "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                            check=True, creationflags=_NO_WINDOW)
    payload = json.loads(result.stdout or "{}")
    stream = (payload.get("streams") or [{}])[0]
    fps = 30.0
    try:
        num, den = str(stream.get("avg_frame_rate") or "30/1").split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except (ValueError, ZeroDivisionError):
        pass
    return {
        "duration_s": round(float((payload.get("format") or {}).get("duration") or 0), 3),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 3),
    }


def _grab_frame(path: Path, at_s: float, height: int) -> bytes:
    command = [_ffmpeg("ffmpeg"), "-v", "error", "-ss", f"{max(0.0, at_s):.3f}",
               "-i", str(path), "-frames:v", "1", "-vf", f"scale=-2:{int(height)}",
               "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=60,
                            creationflags=_NO_WINDOW)
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError("frame_extract_failed")
    return result.stdout


def _waveform(path: Path, duration_s: float, progress=None) -> dict:
    """RMS theo từng khung 0.25 s, chuẩn hoá 0..255 theo phân vị 99."""
    command = [_ffmpeg("ffmpeg"), "-v", "error", "-i", str(path), "-vn", "-ac", "1",
               "-ar", str(WAVEFORM_RATE), "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=_NO_WINDOW)
    bucket = int(WAVEFORM_RATE * WAVEFORM_BUCKET_S)
    values, carry = [], np.empty(0, dtype=np.float32)
    total_expected = max(1.0, duration_s * WAVEFORM_RATE)
    read = 0
    try:
        while True:
            chunk = process.stdout.read(WAVEFORM_RATE * 2 * 30)
            if not chunk:
                break
            samples = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            read += len(samples)
            samples = np.concatenate([carry, samples])
            usable = len(samples) // bucket * bucket
            if usable:
                frames = samples[:usable].reshape(-1, bucket)
                values.extend(np.sqrt((frames * frames).mean(axis=1)).tolist())
            carry = samples[usable:]
            if progress:
                progress(min(0.99, read / total_expected))
    finally:
        process.stdout.close()
        process.wait(timeout=30)
    if len(carry):
        values.append(float(np.sqrt((carry * carry).mean())))
    array = np.asarray(values, dtype=np.float32)
    scale = float(np.percentile(array, 99)) if len(array) else 1.0
    scale = scale if scale > 1e-6 else 1.0
    peaks = np.clip(array / scale * 255.0, 0, 255).astype(np.uint8)
    # Ngưỡng "có tiếng nói": trên 18% đỉnh chuẩn hoá trong ít nhất 0.5 s.
    return {"bucket_s": WAVEFORM_BUCKET_S, "count": int(len(peaks)),
            "peaks": peaks.tolist()}


class VideoAssetManager:
    """Tạo và phục vụ poster / filmstrip / waveform cho một file video."""

    def __init__(self, cache_root: Path):
        self.root = Path(cache_root) / "video"
        self._lock = threading.Lock()
        self._queue: list[tuple[str, Path]] = []
        self._status: dict[str, dict] = {}
        self._worker: threading.Thread | None = None

    def _dir(self, key: str) -> Path:
        return self.root / key

    def _manifest(self, key: str) -> dict | None:
        path = self._dir(key) / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def describe(self, media_path: Path, *, ensure=True, library_revision: int = 0) -> dict:
        media_path = Path(media_path)
        if not media_path.is_file():
            return {"ok": False, "state": "missing_media"}
        key = media_key(media_path, library_revision)
        manifest = self._manifest(key)
        if manifest and manifest.get("complete"):
            return {"ok": True, "state": "ready", "key": key, **manifest}
        with self._lock:
            status = dict(self._status.get(key) or {})
            queued = any(item[0] == key for item in self._queue)
            if ensure and not status.get("running") and not queued and status.get("state") != "error":
                self._queue.append((key, media_path))
                status = {"state": "queued", "progress": 0}
                self._status[key] = status
                self._kick()
        partial = manifest or {}
        return {"ok": True, "key": key, **partial,
                "state": status.get("state", "queued"),
                "progress": status.get("progress", 0),
                "error": status.get("error", "")}

    def file(self, key: str, name: str) -> Path:
        if not key.isalnum() or name not in {"poster.jpg", "filmstrip.jpg", "waveform.json"}:
            raise ValueError("asset_not_found")
        path = self._dir(key) / name
        if not path.is_file():
            raise ValueError("asset_not_found")
        return path

    def retry(self, media_path: Path, library_revision: int = 0):
        key = media_key(Path(media_path), library_revision)
        with self._lock:
            self._status.pop(key, None)
        return self.describe(media_path, library_revision=library_revision)

    def _kick(self):
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="video-assets", daemon=True)
        self._worker.start()

    def _set(self, key, **changes):
        with self._lock:
            self._status.setdefault(key, {}).update(changes)

    def _run(self):
        while True:
            with self._lock:
                if not self._queue:
                    return
                key, media_path = self._queue.pop(0)
                self._status[key] = {"state": "running", "running": True, "progress": 0}
            try:
                self._build(key, media_path)
                self._set(key, state="ready", running=False, progress=1)
            except Exception as exc:  # noqa: BLE001 - trạng thái lỗi phải về được UI
                self._set(key, state="error", running=False,
                          error=f"{type(exc).__name__}: {exc}"[:300])

    def _build(self, key: str, media_path: Path):
        folder = self._dir(key)
        folder.mkdir(parents=True, exist_ok=True)
        info = probe(media_path)
        duration = info["duration_s"]
        if duration <= 0:
            raise RuntimeError("video_duration_unknown")
        manifest = {"media": info, "complete": False, "built_at": time.time()}

        poster = _grab_frame(media_path, min(duration * 0.12, max(0.0, duration - 1)), POSTER_H)
        _atomic_bytes(folder / "poster.jpg", poster)
        manifest["poster"] = True
        _atomic_json(folder / "manifest.json", manifest)
        self._set(key, state="running", progress=0.05, stage="poster")

        interval = max(FILMSTRIP_MIN_INTERVAL_S, duration / FILMSTRIP_MAX_FRAMES)
        times = [round(t, 3) for t in np.arange(0, max(0.0, duration - 0.5), interval)]
        frames: list[Image.Image | None] = [None] * len(times)
        done = [0]

        def work(index):
            data = _grab_frame(media_path, times[index] + min(0.5, interval / 2), FILMSTRIP_FRAME_H)
            frames[index] = Image.open(io.BytesIO(data)).convert("RGB")
            done[0] += 1
            self._set(key, progress=0.05 + 0.6 * done[0] / max(1, len(times)), stage="filmstrip")

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(work, range(len(times))))
        valid = [frame for frame in frames if frame is not None]
        if not valid:
            raise RuntimeError("filmstrip_empty")
        frame_w, frame_h = valid[0].size
        cols = min(FILMSTRIP_COLS, len(frames))
        rows = (len(frames) + cols - 1) // cols
        sheet = Image.new("RGB", (frame_w * cols, frame_h * rows), (15, 23, 42))
        for index, frame in enumerate(frames):
            if frame is None:
                continue
            if frame.size != (frame_w, frame_h):
                frame = ImageOps.fit(frame, (frame_w, frame_h))
            sheet.paste(frame, ((index % cols) * frame_w, (index // cols) * frame_h))
        buffer = io.BytesIO()
        sheet.save(buffer, "JPEG", quality=72, optimize=True)
        _atomic_bytes(folder / "filmstrip.jpg", buffer.getvalue())
        manifest["filmstrip"] = {"interval_s": interval, "count": len(frames), "cols": cols,
                                 "rows": rows, "frame_w": frame_w, "frame_h": frame_h,
                                 "times": times}
        _atomic_json(folder / "manifest.json", manifest)

        wave = _waveform(media_path, duration,
                         progress=lambda p: self._set(key, progress=0.65 + 0.34 * p,
                                                      stage="waveform"))
        _atomic_json(folder / "waveform.json", wave)
        manifest["waveform"] = {"bucket_s": wave["bucket_s"], "count": wave["count"]}
        manifest["complete"] = True
        _atomic_json(folder / "manifest.json", manifest)


def _download(url: str, timeout=8) -> bytes:
    """Tải ảnh ngoài (B4): chặn IP nội bộ/CGNAT kể cả qua redirect, ≤12 MB, ≤30 s."""
    from product_ingest import read_public_url
    return read_public_url(url, 12 * 1024 * 1024, timeout=timeout, total_timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KidTE/1.1",
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
    })


def normalize_image(data: bytes, max_side=PRODUCT_THUMB_MAX) -> bytes:
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("image_invalid") from exc
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((max_side, max_side))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=84, optimize=True)
    return buffer.getvalue()


def product_cover_sources(raw_data):
    """Keep only usable cover URLs, rather than the full TikTok API response."""
    sources = {}
    products = raw_data.get("products") if isinstance(raw_data, dict) else None
    for product in products if isinstance(products, list) else []:
        if not isinstance(product, dict):
            continue
        base = product.get("base") or product.get("product") or {}
        base = base if isinstance(base, dict) else {}
        cover = product.get("cover") or base.get("cover")
        if not isinstance(cover, dict):
            continue
        urls = []
        for field in ("url_list", "thumb_url_list"):
            values = cover.get(field) or []
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                urls.extend(u for u in values if isinstance(u, str)
                            and u.startswith(("http://", "https://")))
        pid = str(product.get("product_id") or product.get("productId")
                  or base.get("product_id") or base.get("productId") or "")
        if pid and urls:
            sources[pid] = list(dict.fromkeys(urls))[:4]
    return sources


class ProductImageCache:
    """Ảnh sản phẩm lưu trên máy: ảnh gian hàng TikTok, rồi ảnh website."""

    def __init__(self, cache_root: Path):
        self.root = Path(cache_root) / "products"
        self._lock = threading.Lock()
        self._failed: dict[str, tuple[tuple[str, ...], float]] = {}
        self._pending: dict[str, tuple[str, ...]] = {}

    RETRY_SECONDS = 30

    @staticmethod
    def _urls(urls):
        return tuple(dict.fromkeys(u for u in urls if isinstance(u, str)
                                   and u.startswith(("http://", "https://"))))

    def _retry_blocked(self, product_id, urls):
        failed = self._failed.get(product_id)
        return bool(failed and failed[0] == self._urls(urls)
                    and time.monotonic() - failed[1] < self.RETRY_SECONDS)

    def _path(self, product_id: str) -> Path:
        safe = "".join(ch for ch in str(product_id) if ch.isalnum() or ch in "-_")[:120]
        if not safe:
            raise ValueError("product_id_invalid")
        return self.root / f"{safe}.jpg"

    def cached(self, product_id: str) -> Path | None:
        path = self._path(product_id)
        return path if path.is_file() else None

    def version(self, product_id: str) -> str:
        path = self.cached(product_id)
        return str(int(path.stat().st_mtime)) if path else ""

    def fetch(self, product_id: str, urls: list[str]) -> Path | None:
        path = self._path(product_id)
        if path.is_file():
            return path
        urls = self._urls(urls)
        # A request before the cart supplies covers must not poison later requests.
        if not urls:
            return None
        with self._lock:
            if self._retry_blocked(product_id, urls):
                return None
        for url in urls:
            try:
                data = normalize_image(_download(url))
            except Exception:  # noqa: BLE001 - thử nguồn tiếp theo
                continue
            _atomic_bytes(path, data)
            with self._lock:
                self._failed.pop(product_id, None)
            return path
        with self._lock:
            if not path.is_file():
                self._failed[product_id] = (urls, time.monotonic())
        return path if path.is_file() else None

    def prefetch(self, sources: dict[str, list[str]]):
        """Tải nền những ảnh còn thiếu; không chặn request đang phục vụ."""
        with self._lock:
            missing = {}
            for pid, values in sources.items():
                urls = self._urls(values)
                try:
                    cached = self.cached(pid)
                except ValueError:
                    continue  # malformed IDs must not strand valid entries already selected
                if not urls or cached or self._retry_blocked(pid, urls):
                    continue
                if pid not in self._pending:
                    missing[pid] = urls
                # Keep a refreshed signed URL even when the old request is queued/running.
                self._pending[pid] = urls
            if not missing:
                return 0

        def run():
            for pid in missing:
                while True:
                    with self._lock:
                        urls = self._pending[pid]
                    try:
                        self.fetch(pid, urls)
                    except Exception as exc:  # one local write failure must not strand the batch
                        logging.getLogger(__name__).warning(
                            "Product image prefetch failed for %s (%s)", pid, type(exc).__name__)
                    finally:
                        with self._lock:
                            finished = self._pending.get(pid) == urls
                            if finished:
                                self._pending.pop(pid, None)
                    if finished:
                        break

        try:
            threading.Thread(target=run, name="product-images", daemon=True).start()
        except Exception:
            with self._lock:
                for pid in missing:
                    self._pending.pop(pid, None)
            raise
        return len(missing)
