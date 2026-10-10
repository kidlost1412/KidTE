"""Model chép lời (faster-whisper): gợi ý theo cấu hình máy, tải, chọn, xoá.

Không đóng gói model vào bộ cài: máy mạnh dùng model lớn, máy yếu dùng model
nhỏ. Model tải thẳng từ Hugging Face về ``DuLieu/models/<id>/`` (có thể tạm
dừng giữa chừng và tải tiếp), hoặc chỉ tới một thư mục model có sẵn.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import DATA_DIR, WHISPER_MODELS_DIR

HF = "https://huggingface.co"
SELECTION_PATH = DATA_DIR / "machine" / "whisper.json"
REQUIRED = ("config.json", "model.bin", "tokenizer.json")
_SKIP = {".gitattributes", "README.md"}

# Thứ tự = thứ tự hiện trên màn hình. ``bytes`` là kích thước model.bin thật
# trên Hugging Face (09/2026) để người dùng biết trước phải tải bao nhiêu.
CATALOG = [
    {"id": "large-v3-turbo", "repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
     "name": "Large v3 Turbo", "bytes": 1_622_000_000, "ram_gb": 6, "quality": 4, "speed": 3,
     "note": "Chép tiếng Việt tốt, nhanh gấp ~4 lần Large v3. Khuyên dùng cho máy có card NVIDIA hoặc RAM từ 8 GB."},
    {"id": "large-v3", "repo": "Systran/faster-whisper-large-v3",
     "name": "Large v3", "bytes": 3_091_000_000, "ram_gb": 10, "quality": 5, "speed": 1,
     "note": "Chính xác nhất nhưng rất chậm nếu không có card NVIDIA từ 6 GB."},
    {"id": "medium", "repo": "Systran/faster-whisper-medium",
     "name": "Medium", "bytes": 1_532_000_000, "ram_gb": 6, "quality": 3, "speed": 2,
     "note": "Chậm hơn Turbo mà không chính xác hơn — chỉ dùng khi Turbo lỗi trên máy này."},
    {"id": "small", "repo": "Systran/faster-whisper-small",
     "name": "Small", "bytes": 486_000_000, "ram_gb": 3, "quality": 2, "speed": 4,
     "note": "Cho máy yếu (RAM 4–8 GB, không card rời). Chép được nhưng hay sai tên sản phẩm — cần rà kỹ."},
    {"id": "base", "repo": "Systran/faster-whisper-base",
     "name": "Base", "bytes": 148_000_000, "ram_gb": 2, "quality": 1, "speed": 5,
     "note": "Rất nhẹ nhưng tiếng Việt kém. Chỉ để thử máy, không nên dùng làm lịch ghim thật."},
]
BY_ID = {item["id"]: item for item in CATALOG}
# Máy cũ tải model qua bộ nhớ đệm Hugging Face (models--<org>--<name>/snapshots/<hash>).
_LEGACY_CACHE = {"large-v3-turbo": "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"}


def _is_model_dir(path):
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED) and (
        (path / "vocabulary.txt").is_file() or (path / "vocabulary.json").is_file())


def installed_path(model_id):
    folder = WHISPER_MODELS_DIR / model_id
    if _is_model_dir(folder) and (folder / ".complete").is_file():
        return folder
    legacy = _LEGACY_CACHE.get(model_id)
    if legacy:
        snapshots = sorted((WHISPER_MODELS_DIR / legacy / "snapshots").glob("*"))
        for candidate in reversed(snapshots):
            if _is_model_dir(candidate):
                return candidate
    return None


def _folder_bytes(path):
    try:
        return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())
    except OSError:
        return 0


def load_selection():
    try:
        data = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_selection(data):
    SELECTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SELECTION_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SELECTION_PATH)


def active_model_path():
    """Đường dẫn model sẽ dùng để chép lời, hoặc None nếu máy chưa có model nào."""
    explicit = (os.getenv("KID_TE_WHISPER_MODEL", "") or os.getenv("LIVE_RE_WHISPER_MODEL", "")).strip()
    if explicit:
        return explicit
    selection = load_selection()
    if selection.get("custom_path") and _is_model_dir(Path(selection["custom_path"])):
        return selection["custom_path"]
    chosen = installed_path(selection.get("model", ""))
    if chosen:
        return str(chosen)
    for item in CATALOG:                      # chưa chọn: lấy model tốt nhất đang có
        found = installed_path(item["id"])
        if found:
            return str(found)
    return None


def _nvidia_gpu():
    """Tên + VRAM card NVIDIA (nếu có). Không có nvidia-smi thì coi như không có."""
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not output:
        return None
    name, _, memory = output[0].partition(",")
    try:
        vram = round(int(memory.strip()) / 1024, 1)
    except ValueError:
        vram = 0
    return {"name": name.strip(), "vram_gb": vram}


def _cuda_ready():
    """Card NVIDIA có DÙNG ĐƯỢC để chép lời không.

    Có card chưa đủ: faster-whisper còn cần cuBLAS + cuDNN. Thiếu thì lần chép đầu
    tiên lỗi rồi tự lùi về CPU — ước lượng thời gian phải theo CPU.
    """
    try:
        import sys
        if getattr(sys, "frozen", False):
            from transcription_client import gpu_available
            return gpu_available()
        from video_analysis import _register_cuda_dll_dirs
        _register_cuda_dll_dirs()
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        if os.name == "nt":
            import ctypes
            for name in ("cublas64_12.dll", "cudnn64_9.dll"):
                ctypes.WinDLL(name)
        return True
    except Exception:
        return False


_HARDWARE = {}


def hardware(refresh=False):
    if _HARDWARE and not refresh:
        return dict(_HARDWARE)
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / 1024 ** 3, 1)
    except Exception:
        ram = 0
    gpu = _nvidia_gpu()
    try:
        free = round(shutil.disk_usage(WHISPER_MODELS_DIR.parent if not WHISPER_MODELS_DIR.exists()
                                       else WHISPER_MODELS_DIR).free / 1024 ** 3, 1)
    except OSError:
        free = 0
    _HARDWARE.update({"ram_gb": ram, "cpu_cores": os.cpu_count() or 0, "gpu": gpu,
                      "cuda": bool(gpu) and _cuda_ready(), "disk_free_gb": free})
    return dict(_HARDWARE)


def gpu_usable(hw):
    """GPU LUÔN là lựa chọn đầu tiên: card NVIDIA từ 2 GB + CUDA chạy được."""
    return bool(hw.get("cuda")) and (hw.get("gpu") or {}).get("vram_gb", 0) >= 2


def recommend(hw):
    if gpu_usable(hw):
        return "large-v3-turbo"             # Turbo int8 trên GPU chỉ cần ~1,5 GB VRAM
    ram = hw.get("ram_gb", 0)
    if ram >= 8:
        return "large-v3-turbo"
    if ram >= 4:
        return "small"
    return "base"


def estimate_minutes(model_id, hw, video_minutes=60):
    """Ước lượng thô thời gian chép lời một video 60 phút trên máy này."""
    fast = gpu_usable(hw)
    per_hour = {  # phút cho 1 giờ video: (card NVIDIA, chỉ CPU)
        "large-v3-turbo": (4, 45), "large-v3": (10, 150), "medium": (8, 90),
        "small": (3, 20), "base": (2, 8),
    }[model_id]
    return round(per_hour[0 if fast else 1] * video_minutes / 60)


class ModelDownloader:
    """Tải một model trong luồng nền; tải tiếp từ byte đã có nếu bị ngắt."""

    def __init__(self):
        self._lock = threading.Lock()
        self._job = {}
        self._thread = None
        self._cancel = threading.Event()

    def status(self):
        with self._lock:
            return dict(self._job)

    def start(self, model_id):
        item = BY_ID.get(model_id)
        if not item:
            raise ValueError("model_unknown")
        with self._lock:
            # Lượt trước (kể cả đang dừng dở) phải thoát hẳn mới cho tải tiếp,
            # nếu không hai luồng cùng ghi một file .part.
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("download_in_progress")
            self._cancel = threading.Event()        # mỗi lượt một cờ dừng riêng
            self._job = {"model": model_id, "state": "downloading", "done": 0,
                         "total": item["bytes"], "file": "", "error": "", "started_at": time.time()}
            self._thread = threading.Thread(target=self._run, args=(item, self._cancel), daemon=True,
                                            name=f"whisper-download-{model_id}")
            self._thread.start()
        return self.status()

    def cancel(self, wait_s=10):
        self._cancel.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=wait_s)             # trả về khi đã dừng thật
        return self.status()

    def _update(self, **fields):
        with self._lock:
            self._job.update(fields)

    def _run(self, item, cancel):
        target = WHISPER_MODELS_DIR / item["id"]
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / ".complete").unlink(missing_ok=True)
            files = self._list_files(item["repo"])
            total = sum(size for _name, size in files)
            self._update(total=total)
            done = 0
            for name, size in files:
                self._update(file=name)
                done = self._download(item["repo"], name, size, target / name, done, cancel)
            (target / ".complete").write_text(json.dumps(
                {"repo": item["repo"], "files": dict(files), "at": time.time()}), encoding="utf-8")
            self._update(state="done", done=total, file="")
            selection = load_selection()
            if not installed_path(selection.get("model", "")) and not selection.get("custom_path"):
                select(item["id"])                    # model đầu tiên → dùng luôn
        except _Cancelled:
            self._update(state="cancelled")
        except Exception as exc:
            from problems import report_problem
            report_problem("whisper_download_failed", f"Tải model {item['id']} lỗi", exc, severity="warning")
            self._update(state="error", error=_friendly(exc))

    @staticmethod
    def _open(url, headers=None, timeout=30):
        request = urllib.request.Request(url, headers={"User-Agent": "KidTE", **(headers or {})})
        from net import ssl_context
        return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())

    def _list_files(self, repo):
        with self._open(f"{HF}/api/models/{repo}/tree/main") as response:
            rows = json.loads(response.read().decode("utf-8"))
        files = [(row["path"], int(row.get("size") or 0)) for row in rows
                 if row.get("type") == "file" and row.get("path") not in _SKIP and "/" not in row["path"]]
        if not {name for name, _ in files} >= set(REQUIRED):
            raise RuntimeError("Kho model trên Hugging Face thiếu file cần thiết")
        return files

    # Mạng chậm/rớt giữa file 1,6 GB là chuyện thường: tự tải tiếp từ byte đang có,
    # chỉ báo lỗi khi đã thử lại nhiều lần mà vẫn không tiến thêm được.
    RETRIES = 8

    def _download(self, repo, name, size, destination, done_before, cancel):
        import http.client
        attempt, last_have = 0, -1
        while True:
            try:
                return self._download_once(repo, name, size, destination, done_before, cancel)
            except _Cancelled:
                raise
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, _ShortRead) as exc:
                partial = destination.with_name(destination.name + ".part")
                have = partial.stat().st_size if partial.is_file() else 0
                attempt = 0 if have > last_have else attempt + 1   # có tiến triển thì đếm lại
                last_have = have
                if attempt >= self.RETRIES:
                    raise exc
                if cancel.wait(min(30, 2 ** attempt)):
                    raise _Cancelled()

    def _download_once(self, repo, name, size, destination, done_before, cancel):
        if destination.is_file() and destination.stat().st_size == size:
            self._update(done=done_before + size)
            return done_before + size
        partial = destination.with_name(destination.name + ".part")
        have = partial.stat().st_size if partial.is_file() else 0
        if have > size:
            partial.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with self._open(f"{HF}/{repo}/resolve/main/{name}", headers, timeout=60) as response:
            if have and response.status != 206:        # máy chủ không cho tải tiếp → tải lại từ đầu
                have = 0
            with partial.open("ab" if have else "wb") as handle:
                while True:
                    if cancel.is_set():
                        raise _Cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    have += len(chunk)
                    self._update(done=done_before + have)
        if have != size:
            raise _ShortRead(f"{name}: tải được {have} / {size} byte — mạng bị ngắt, bấm Tải tiếp")
        partial.replace(destination)
        return done_before + size


class _Cancelled(Exception):
    pass


class _ShortRead(RuntimeError):
    pass


def _friendly(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"Hugging Face trả lỗi {exc.code}. Thử lại sau, hoặc tải tay theo đường dẫn."
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        from net import classify
        info = classify(exc, "huggingface.co")
        return f"{info['message']}. Bấm Tải tiếp (phần đã tải được giữ). [{info['detail']}]"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "Ổ đĩa hết chỗ trống."
    return str(exc)[:300]


def select(model_id="", custom_path=""):
    if custom_path:
        path = Path(custom_path.strip().strip('"'))
        if not _is_model_dir(path):
            raise ValueError("Thư mục này không phải model faster-whisper (cần config.json, model.bin, "
                             "tokenizer.json, vocabulary.*)")
        _save_selection({"custom_path": str(path)})
        return
    if not installed_path(model_id):
        raise ValueError("model_not_installed")
    _save_selection({"model": model_id})


def delete(model_id):
    if model_id not in BY_ID:
        raise ValueError("model_unknown")
    removed = False
    for folder in (WHISPER_MODELS_DIR / model_id, WHISPER_MODELS_DIR / _LEGACY_CACHE.get(model_id, "\0")):
        if folder.is_dir() and folder.resolve().parent == WHISPER_MODELS_DIR.resolve():
            shutil.rmtree(folder)
            removed = True
    if load_selection().get("model") == model_id:
        _save_selection({})
    return removed


def state(downloader):
    hw = hardware()
    active = active_model_path()
    selection = load_selection()
    job = downloader.status()
    models = []
    for item in CATALOG:
        path = installed_path(item["id"])
        partial = (WHISPER_MODELS_DIR / item["id"]).is_dir() and not path
        models.append({
            **{key: item[key] for key in ("id", "name", "bytes", "ram_gb", "quality", "speed", "note")},
            "installed": bool(path), "path": str(path or ""),
            "disk_bytes": _folder_bytes(path) if path else 0,
            "partial": partial,
            "active": bool(path) and active == str(path),
            "page_url": f"{HF}/{item['repo']}",
            "minutes_per_hour": estimate_minutes(item["id"], hw),
            "fits": hw.get("ram_gb", 0) >= item["ram_gb"] or (
                hw.get("cuda") and (hw.get("gpu") or {}).get("vram_gb", 0) >= item["ram_gb"] / 2),
        })
    return {"ok": True, "hardware": hw, "recommended": recommend(hw), "models": models,
            "device": "gpu" if gpu_usable(hw) else "cpu",
            "active_path": active or "", "custom_path": selection.get("custom_path", ""),
            "env_override": bool((os.getenv("KID_TE_WHISPER_MODEL", "") or os.getenv("LIVE_RE_WHISPER_MODEL", "")).strip()),
            "folder": str(WHISPER_MODELS_DIR), "download": job}
