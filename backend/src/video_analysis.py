"""Upload, phan tich va duyet video phat lai truoc khi LIVE.

May chi tao BAN NHAP: fingerprint + transcript + goi y SKU. Timeline chi duoc
dua vao thu vien sau khi nguoi van hanh bam phat hanh mot ban hop le.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config import ROOT
import gpu_runtime
from video_fingerprint import (
    TARGET_RATE, read_pcm16, resample_mono, save_reference, spectral_features,
)
from video_library import VIDEO_DIR, validate_timeline


MAX_VIDEO_BYTES = 20 * 1024**3
MAX_CHUNK_BYTES = 16 * 1024**2
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_CUDA_DLL_HANDLES = gpu_runtime._CUDA_DLL_HANDLES


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fold(value):
    text = unicodedata.normalize(
        "NFD", str(value or "").casefold().replace("đ", "d")
    )
    return " ".join("".join(
        char if (char.isalnum() or char.isspace()) else " "
        for char in text if unicodedata.category(char) != "Mn"
    ).split())


def _safe_name(value):
    name = Path(str(value or "video.mp4")).name
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-") or "video"
    suffix = Path(name).suffix.casefold()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("video_extension_not_supported")
    return stem[:80] + suffix


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _ffmpeg_binary(name):
    from config import find_tool
    found = find_tool(name)
    if not found:
        raise RuntimeError(f"missing_{name}")
    return found


def _probe_video(path):
    command = [
        _ffmpeg_binary("ffprobe"), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
    payload = json.loads(result.stdout)
    duration = float((payload.get("format") or {}).get("duration") or 0)
    streams = payload.get("streams") or []
    if duration <= 1 or not any(row.get("codec_type") == "audio" for row in streams):
        raise ValueError("video_requires_audio_track")
    if not any(row.get("codec_type") == "video" for row in streams):
        raise ValueError("video_track_missing")
    return {"duration_s": round(duration, 3), "streams": streams}


def _extract_audio(video, wav):
    subprocess.run([
        _ffmpeg_binary("ffmpeg"), "-y", "-v", "error", "-i", str(video),
        "-vn", "-ac", "1", "-ar", str(TARGET_RATE), "-acodec", "pcm_s16le",
        str(wav),
    ], timeout=60 * 60, check=True)


def _build_fingerprint(wav, destination):
    samples, rate = read_pcm16(wav)
    samples, rate = resample_mono(samples, rate)
    features = spectral_features(samples, rate)
    duration = len(samples) / rate
    save_reference(destination, features, duration, wav)
    return round(duration, 3), int(features.shape[0])


def _find_whisper_model():
    """Model chép lời đã chọn ở Cài đặt → Chép lời (None nếu máy chưa có model)."""
    from whisper_models import active_model_path
    return active_model_path()


def _register_cuda_dll_dirs():
    """Register the validated opt-in pack or the established legacy runtime."""
    return gpu_runtime.register_cuda_dll_dirs()


def _transcribe(path):
    # CTranslate2 resolves CUDA DLLs while faster_whisper is imported. Windows
    # therefore needs the workspace/runtime directories registered first;
    # registering them after the import makes model loading appear successful
    # but fails on the first encoder operation and silently falls back to CPU.
    gpu_error = None
    try:
        _register_cuda_dll_dirs()
    except gpu_runtime.GpuRuntimeError as exc:
        # An explicit but invalid pack must not fall through to a system CUDA
        # runtime or to a source venv. Keep transcription available on CPU.
        gpu_error = exc
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster_whisper_not_installed") from exc
    model_name = _find_whisper_model()
    if not model_name:
        # Không để faster-whisper tự tải ngầm 1,6 GB vào ổ C: người dùng chọn
        # model hợp với máy ở Cài đặt → Chép lời.
        raise RuntimeError("Máy chưa có model chép lời — vào Cài đặt → Chép lời để tải model")
    # WAV đã được chính Kid TE chuẩn hóa PCM16/16 kHz. Truyền mảng float32
    # thẳng cho faster-whisper để không buộc PyAV trong bản đóng gói phải mở
    # lại file; trên Windows frozen build, lớp PyAV có thể báo NoSuchFile dù
    # WAV tồn tại và vừa được đọc thành công ở bước fingerprint.
    audio, rate = read_pcm16(path)
    audio, _ = resample_mono(audio, rate)
    if gpu_error is not None:
        from problems import report_problem
        report_problem(
            "whisper_gpu_fallback",
            "Gói GPU không hợp lệ hoặc không tương thích; chép lời bằng CPU (chậm hơn)",
            gpu_error, severity="warning",
        )
    last_error = None
    devices = (("cpu", "int8"),) if gpu_error is not None else (
        ("cuda", "int8_float16"), ("cpu", "int8"),
    )
    for device, compute in devices:
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute,
                                 local_files_only=Path(model_name).exists())
            segments, info = model.transcribe(
                audio, language="vi", vad_filter=True, beam_size=5,
                condition_on_previous_text=True,
            )
            rows = [{
                "start": round(float(item.start), 3),
                "end": round(float(item.end), 3),
                "text": str(item.text or "").strip(),
            } for item in segments]
            return rows, {"language": info.language,
                          "language_probability": round(float(info.language_probability), 4),
                          "device": device, "compute_type": compute,
                          "model": model_name}
        except Exception as exc:  # GPU co the thieu DLL; CPU la duong lui an toan.
            last_error = exc
            if device == "cuda":
                from problems import report_problem
                report_problem("whisper_gpu_fallback", "Chép lời bằng GPU lỗi, chuyển sang CPU (chậm hơn)",
                               exc, severity="warning")
    raise RuntimeError(f"whisper_failed:{type(last_error).__name__}") from last_error


def _catalog_rows(snapshot):
    rows = []
    for item in (snapshot or {}).get("products") or []:
        product_id = str(item.get("product_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not product_id or not title:
            continue
        rows.append({
            "product_id": product_id,
            "bag_index": int(item.get("bag_index") or 0),
            "title": title,
            # Ten profile co the cu hoac dung chung cho nhieu quy cach (1L/5L).
            # Man hinh duyet va timeline phai uu tien listing TikTok thuc te.
            "short_name": title[:100],
            "profile_id": str(item.get("profile_id") or ""),
            "profile_name": str(item.get("profile_name") or "")[:100],
            "image_url": str(item.get("image_url") or "")[:2000],
            "price": str(item.get("price") or item.get("original_price") or "")[:100],
        })
    return rows


def _product_anchors(products):
    """Lay neo tu TEN listing, khong tu duoi SEO dai va tu noi thong dung.

    TikTok title thuong noi them hang chuc cong dung. Neu dung toan bo title,
    cac tu nhu "khong", "pha", "binh" tao rat nhieu goi y sai. 12 token dau
    chua ten/dong/quy cach va on dinh hon nhieu.
    """
    ignored = {
        "san", "pham", "chai", "can", "cho", "cay", "giup", "nong", "duoc",
        "toan", "cau", "phan", "bon", "huu", "cong", "nghe", "sieu", "dang",
        "moi", "cac", "loai", "trong", "chuyen", "dung", "manh",
        "xanh", "la", "phat", "trien", "cuc", "chat", "dinh", "duong",
        "mai", "vang", "hap", "thu", "nhanh", "cai", "tien", "thoi", "than",
        "bao", "day", "cung", "hanh", "vua",
    }
    raw, spread = {}, Counter()
    for row in products:
        prefix = re.split(r"\s(?:[-–|])\s|[|]", row["title"], maxsplit=1)[0]
        folded_prefix = re.split(
            r"\b(?:giup|cong nghe|bo sung|phuc hoi|phat trien|chuyen dung)\b",
            _fold(prefix), maxsplit=1,
        )[0]
        prefix_tokens = folded_prefix.split()[:12]
        ordered = [token for token in prefix_tokens
                   if len(token) >= 3 and token not in ignored]
        tokens = set(ordered)
        raw[row["product_id"]] = (ordered, tokens, prefix_tokens)
        spread.update(tokens)
    result = {}
    for product_id, (_ordered, tokens, prefix_tokens) in raw.items():
        rare = {token for token in tokens if spread[token] <= 2}
        phrases = set()
        for size in (2, 3):
            for index in range(len(prefix_tokens) - size + 1):
                window = prefix_tokens[index:index + size]
                if any(len(token) < 3 or token in ignored for token in window):
                    continue
                phrase = " ".join(window)
                if len(phrase) >= 7:
                    phrases.add(phrase)
        result[product_id] = {"rare": rare, "phrases": phrases}
    phrase_spread = Counter(phrase for value in result.values()
                            for phrase in value["phrases"])
    for value in result.values():
        value["phrases"] = {phrase for phrase in value["phrases"]
                            if phrase_spread[phrase] <= 2}
    return result


def _propose_segments(segments, products):
    """Goi y bao thu: chi chon khi co tu rieng; moi goi y van can nguoi duyet."""
    anchors = _product_anchors(products)
    by_id = {row["product_id"]: row for row in products}
    scored = []
    for segment_index, segment in enumerate(segments):
        text = _fold(segment.get("text"))
        tokens = set(text.split())
        ranked = []
        for product_id, wanted in anchors.items():
            hits = sorted(tokens & wanted["rare"])
            phrase_hits = sorted(phrase for phrase in wanted["phrases"]
                                 if f" {phrase} " in f" {text} ")
            strong = [token for token in hits
                      if re.search(r"[a-z]", token) and re.search(r"\d", token)
                      and not re.fullmatch(r"\d+(?:ml|kg|gr|lit)", token)]
            # Mot tu noi/SEO khong du bang chung. Chap nhan ma san pham,
            # cum ten lien tiep, hoac it nhat hai neo hiem cung xuat hien.
            if not strong and not phrase_hits and len(hits) < 2:
                continue
            score = len(hits) + 4 * len(strong) + 3 * len(phrase_hits)
            evidence_terms = list(dict.fromkeys(strong + phrase_hits + hits))
            ranked.append((score, product_id, evidence_terms))
        ranked.sort(reverse=True)
        if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
            continue
        score, product_id, hits = ranked[0]
        scored.append({
            "at_ms": int(float(segment.get("start", 0)) * 1000),
            "end_ms": int(float(segment.get("end", 0)) * 1000),
            "product_id": product_id,
            "confidence": "high" if score >= 4 else "medium",
            "matched_terms": hits[:8],
            "evidence": str(segment.get("text") or "").strip()[:300],
            "title": by_id[product_id]["title"],
            "context_before": " ".join(str(x.get("text") or "").strip()
                                       for x in segments[max(0, segment_index - 2):segment_index])[-600:],
            "context_after": " ".join(str(x.get("text") or "").strip()
                                      for x in segments[segment_index + 1:segment_index + 3])[:600],
            "candidates": [{"product_id": item[1], "score": item[0],
                            "matched_terms": item[2][:6]} for item in ranked[:3]],
        })
    blocks = []
    for row in scored:
        if blocks and blocks[-1]["product_id"] == row["product_id"] \
                and row["at_ms"] - blocks[-1]["end_ms"] <= 45000:
            blocks[-1]["end_ms"] = row["end_ms"]
            blocks[-1]["evidence"] = (blocks[-1]["evidence"] + " " + row["evidence"])[:500]
            blocks[-1]["matched_terms"] = sorted(set(
                blocks[-1]["matched_terms"] + row["matched_terms"]
            ))[:10]
        else:
            blocks.append(dict(row))
    events = []
    for row in blocks:
        if events and row["at_ms"] - events[-1]["at_ms"] < 20000:
            continue
        events.append(row)
    folded_titles = {product_id: _fold(item["title"]) for product_id, item in by_id.items()}
    combo_ids = [product_id for product_id, title in folded_titles.items()
                 if "combo" in title and "dam ca" in title and "humic" in title]
    humic_5l_ids = [product_id for product_id, title in folded_titles.items()
                    if "humic" in title and re.search(r"(?:^| )5 ?(?:l|lit)(?: |$)", title)
                    and "combo" not in title]
    if combo_ids and humic_5l_ids:
        for row in events:
            if row["product_id"] in combo_ids and row["end_ms"] - row["at_ms"] > 120000:
                row["alternate_product_id"] = humic_5l_ids[0]
                row["alternate_every_ms"] = 60000
                row["alternation_reason"] = "Đoạn combo dài hơn 2 phút: luân phiên Combo và Humic can 5L"
    return events


def timeline_product_entry(row):
    """Một mục ``products`` của timeline dựng từ một dòng danh mục.

    Dùng chung cho phát hành từ phân tích và cho sửa timeline trong Studio,
    để ``include_all`` (thứ tiện ích dùng đối chiếu tên sản phẩm đang ghim)
    luôn được sinh theo đúng một công thức.
    """
    return {
        "product_id": str(row["product_id"]), "bag_index": int(row.get("bag_index") or 0),
        "short_name": str(row.get("short_name") or row["title"][:100]),
        "title": str(row["title"]),
        "include_all": [_fold(row["title"])[:80]], "exclude_any": [],
    }


class UploadOffsetMismatch(ValueError):
    """Client gui sai offset. Mang theo offset server da commit de resync."""

    def __init__(self, message, committed_offset):
        super().__init__(message)
        self.committed_offset = int(committed_offset)


class UploadPartCorrupt(ValueError):
    """File .part tren dia khong khop metadata; phai upload lai tu dau."""

    def __init__(self, message, actual_size):
        super().__init__(message)
        self.actual_size = int(actual_size)


def _safe_probe(value):
    """Van tay noi dung: 64 ky tu hex thuong, hoac rong."""
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        return ""
    return text


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class VideoAnalysisManager:
    def __init__(self, root=None, catalog_provider=None, live_provider=None):
        self.root = Path(root or (ROOT / "data" / "video_uploads"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog_provider = catalog_provider or (lambda: {})
        self.live_provider = live_provider or (lambda: False)
        self._lock = threading.RLock()
        # Whisper/ffmpeg tốn nhiều CPU/GPU và RAM. Cho phép phân tích trong LIVE
        # theo yêu cầu vận hành, nhưng tuyệt đối không chạy nhiều video song song.
        self._analysis_lock = threading.Lock()
        self._threads = {}
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self):
        """Bien cong viec dang chay luc app tat thanh trang thai co the tiep tuc.

        Khong tu dong khoi dong Whisper khi app vua mo: nguoi van hanh co the
        dang LIVE. UI se cho phep bam tiep tuc vao luc an toan.
        """
        for path in self.root.glob("*/job.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("state") not in ("queued", "analyzing"):
                    continue
                state.update({
                    "state": "interrupted", "phase": "interrupted",
                    "updated_at": _now(),
                    "error": "Phân tích bị gián đoạn khi ứng dụng đóng; có thể tiếp tục.",
                })
                _atomic_json(path, state)
            except (OSError, json.JSONDecodeError):
                continue

    def _dir(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", str(job_id or "")):
            raise ValueError("invalid_job_id")
        return self.root / job_id

    def _read(self, job_id):
        path = self._dir(job_id) / "job.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("job_not_found") from exc
        return value

    def _write(self, job_id, **changes):
        with self._lock:
            current = self._read(job_id)
            current.update(changes)
            current["updated_at"] = _now()
            _atomic_json(self._dir(job_id) / "job.json", current)
            return current

    def start_upload(self, filename, total_size, source_probe=""):
        snapshot = self.catalog_provider() or {}
        catalog_snapshot = _catalog_rows(snapshot)
        if not catalog_snapshot:
            raise ValueError("saved_catalog_required")
        name = _safe_name(filename)
        size = int(total_size)
        if size <= 0 or size > MAX_VIDEO_BYTES:
            raise ValueError("invalid_video_size")
        job_id = uuid.uuid4().hex
        folder = self._dir(job_id)
        folder.mkdir(parents=True)
        state = {
            "ok": True, "job_id": job_id, "state": "uploading", "filename": name,
            "total_size": size, "uploaded_size": 0, "progress": 0,
            # Van tay noi dung do client tinh (dau + cuoi file). Dung de xac
            # minh khi nguoi dung chon lai file de tiep tuc: ten va kich thuoc
            # trung nhau khong chung minh duoc do la CUNG mot video (F06).
            "source_probe": _safe_probe(source_probe),
            "created_at": _now(), "updated_at": _now(),
            "catalog_snapshot": catalog_snapshot,
            "catalog_synced_at": str(snapshot.get("synced_at") or ""),
            "catalog_revision": hashlib.sha256(json.dumps(catalog_snapshot, ensure_ascii=False,
                                                sort_keys=True).encode("utf-8")).hexdigest(),
        }
        _atomic_json(folder / "job.json", state)
        return state

    def resumable_job(self, filename, total_size, source_probe=""):
        """Tim job dang do khop CA ten, kich thuoc VA van tay noi dung.

        Truoc day viec nay do trinh duyet lam, chi so ten + kich thuoc. Hai
        video khac nhau cung ten va cung so byte (vi du mot ban xuat lai) se
        bi noi de len nhau, ma finish_upload khong the phat hien vi kich thuoc
        van dung.
        """
        name = _safe_name(filename)
        size = int(total_size)
        probe = _safe_probe(source_probe)
        if not probe:
            raise ValueError("upload_probe_required")
        ung_vien = []
        for row in self.list_jobs().get("jobs", []):
            if row.get("state") != "uploading":
                continue
            if row.get("filename") != name or int(row.get("total_size") or 0) != size:
                continue
            luu = _safe_probe(row.get("source_probe"))
            if not luu:
                # Job cu chua co van tay: khong the chung minh cung mot file,
                # nen khong cho tiep tuc. Tai lai tu dau van an toan.
                continue
            if luu != probe:
                continue
            ung_vien.append(row)
        ung_vien.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return ung_vien[0] if ung_vien else None

    def append_chunk(self, job_id, offset, stream, length):
        """Ghi mot chunk tai dung offset da commit.

        Ban cu mo file o che do "ab" roi ghi dan. Khi stream dut giua chung,
        nhung byte da ghi VAN NAM LAI trong .part nhung uploaded_size khong
        tang, nen lan retry cung offset se noi them mot ban sao nua -> file
        phinh ra va hong, ma finish_upload van nhan vi no chi so uploaded_size
        voi total_size. Nay ghi theo seek(offset) va truncate ve ranh gioi da
        commit khi that bai, nen retry luon ghi de dung cho, khong nhan doi.
        """
        length, offset = int(length), int(offset)
        if length <= 0 or length > MAX_CHUNK_BYTES:
            raise ValueError("invalid_chunk_size")
        with self._lock:
            state = self._read(job_id)
            committed = int(state.get("uploaded_size") or 0)
            if state.get("state") != "uploading":
                raise UploadOffsetMismatch("upload_offset_mismatch", committed)
            if offset != committed:
                raise UploadOffsetMismatch("upload_offset_mismatch", committed)
            if offset + length > state["total_size"]:
                raise ValueError("upload_exceeds_declared_size")
            part = self._dir(job_id) / (state["filename"] + ".part")
            actual = part.stat().st_size if part.exists() else 0
            if actual < committed:
                # File ngan hon so da xac nhan: metadata va file vat ly lech
                # nhau, khong the noi tiep mot cach an toan.
                raise UploadPartCorrupt("upload_part_truncated_on_disk", actual)
            mode = "r+b" if part.exists() else "w+b"
            with part.open(mode) as handle:
                try:
                    handle.seek(committed)
                    remaining = length
                    while remaining:
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("upload_chunk_truncated")
                        handle.write(block)
                        remaining -= len(block)
                    # Bo moi byte thua phia sau (tan du cua lan ghi truoc).
                    handle.truncate(committed + length)
                    handle.flush()
                    os.fsync(handle.fileno())
                except BaseException:
                    # Quay ve dung ranh gioi chunk da commit truoc do.
                    handle.truncate(committed)
                    handle.flush()
                    os.fsync(handle.fileno())
                    raise
            uploaded = offset + length
            return self._write(job_id, uploaded_size=uploaded,
                               progress=round(uploaded * 100 / state["total_size"], 1))

    def finish_upload(self, job_id, title=""):
        with self._lock:
            state = self._read(job_id)
            if state.get("state") != "uploading" or state["uploaded_size"] != state["total_size"]:
                raise ValueError("upload_incomplete")
            folder = self._dir(job_id)
            part = folder / (state["filename"] + ".part")
            source = folder / state["filename"]
            # Doi chieu file VAT LY voi kich thuoc da khai bao. Ban cu chi tin
            # uploaded_size trong job.json nen mot .part hong/thua byte van
            # duoc chuyen sang trang thai san sang.
            if not part.is_file():
                raise ValueError("upload_part_missing")
            actual = part.stat().st_size
            if actual != state["total_size"]:
                raise UploadPartCorrupt("upload_size_mismatch", actual)
            os.replace(part, source)
            self._write(job_id, state="queued", progress=100, title=str(title or "").strip())
            self._start_worker(job_id)
            return self._read(job_id)

    def _start_worker(self, job_id):
        worker = threading.Thread(target=self._analyze, args=(job_id,), daemon=True,
                                  name=f"video-analysis-{job_id[:8]}")
        self._threads[job_id] = worker
        worker.start()

    def resume(self, job_id):
        if not _catalog_rows(self.catalog_provider()):
            raise ValueError("saved_catalog_required")
        with self._lock:
            state = self._read(job_id)
            if state.get("state") not in ("interrupted", "error"):
                raise ValueError("job_not_resumable")
            source = self._dir(job_id) / state["filename"]
            if not source.is_file():
                raise ValueError("uploaded_video_missing")
            self._write(job_id, state="queued", phase="queued", error="")
            self._start_worker(job_id)
            return self._read(job_id)

    def _analyze(self, job_id):
        with self._analysis_lock:
            self._analyze_one(job_id)

    def _analyze_one(self, job_id):
        folder = self._dir(job_id)
        try:
            state = self._write(job_id, state="analyzing", phase="checking_video")
            video = folder / state["filename"]
            media = _probe_video(video)
            self._write(job_id, phase="extracting_audio", media=media,
                        source_sha256=_sha256(video))
            wav = folder / "audio-16k.wav"
            _extract_audio(video, wav)
            self._write(job_id, phase="building_fingerprint")
            duration, frames = _build_fingerprint(wav, folder / "fingerprint.npz")
            self._write(job_id, phase="transcribing", duration_s=duration,
                        fingerprint_frames=frames)
            segments, transcription = _transcribe(wav)
            _atomic_json(folder / "transcript.json", {
                "segments": segments, "metadata": transcription,
            })
            self._write(job_id, phase="matching_products")
            products = state.get("catalog_snapshot") or _catalog_rows(self.catalog_provider())
            suggestions = _propose_segments(segments, products)
            draft = {
                "schema_version": 1, "job_id": job_id,
                "title": state.get("title") or Path(state["filename"]).stem,
                "source_file": state["filename"], "cycle_ms": int(round(duration * 1000)),
                "repin_every_ms": 20000, "catalog": products,
                "suggestions": suggestions, "events": [{
                    "at_ms": row["at_ms"], "product_id": row["product_id"],
                    "confidence": row["confidence"], "evidence": row["evidence"],
                    "end_ms": row.get("end_ms"), "matched_terms": row.get("matched_terms", []),
                    "context_before": row.get("context_before", ""),
                    "context_after": row.get("context_after", ""),
                    "candidates": row.get("candidates", []),
                    "alternate_product_id": row.get("alternate_product_id", ""),
                    "alternate_every_ms": row.get("alternate_every_ms", 0),
                    "review_status": "pending",
                } for row in suggestions],
                "transcription": transcription,
            }
            _atomic_json(folder / "draft.json", draft)
            try:
                wav.unlink()
            except OSError:
                pass
            self._write(job_id, state="review", phase="owner_review",
                        suggestions=len(suggestions), transcript_segments=len(segments))
        except Exception as exc:
            from problems import report_problem
            report_problem("video_analysis_failed", f"Phân tích video {job_id} lỗi", exc)
            try:
                self._write(job_id, state="error", phase="failed",
                            error=f"{type(exc).__name__}: {exc}"[:500])
            except Exception as ghi_loi:
                # Job co the da bi xoa trong luc phan tich (nguoi dung huy, don
                # thu muc). Ban cu de chinh trinh xu ly loi nem tiep, thanh mot
                # exception khong ai bat trong thread nen -> tien trinh ghi mot
                # traceback tho ra console va khong con dau vet nao de lan.
                print(f"[video-analysis] job {job_id} that bai va khong ghi lai "
                      f"duoc trang thai: {type(exc).__name__}: {exc} "
                      f"({type(ghi_loi).__name__})", flush=True)
        finally:
            self._threads.pop(job_id, None)

    def status(self, job_id, include_draft=False):
        state = self._read(job_id)
        if include_draft and state.get("state") in ("review", "published"):
            try:
                state["draft"] = json.loads(
                    (self._dir(job_id) / "draft.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                state["draft"] = None
        return state

    def list_jobs(self):
        rows = []
        for path in sorted(self.root.glob("*/job.json"), reverse=True):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return {"ok": True, "jobs": rows[:30]}

    def preflight(self):
        snapshot = self.catalog_provider() or {}
        products = _catalog_rows(snapshot)
        synced = str(snapshot.get("synced_at") or "")
        return {
            "ok": True, "catalog_ready": bool(products), "product_count": len(products),
            "synced_at": synced, "room_id": str(snapshot.get("room_id") or ""),
            "can_analyze_offline": bool(products),
            "live_running": bool(self.live_provider()),
            "analysis_model": _find_whisper_model(),
            "max_parallel_jobs": 1,
            "note": ("Danh muc da luu dung duoc khi khong mo LIVE. Truoc moi phien van "
                     "nen quet lai de xac nhan thu tu gio va san pham da thay doi."),
        }

    def publish(self, job_id, title, events, *, operation_id="", expected_revision=""):
        # Publishing is a state transition, not merely a file write.  Keep the
        # read/validate/commit sequence serial per manager so two browser
        # requests cannot each allocate a different immutable video ID.
        with self._lock:
            return self._publish_locked(
                job_id, title, events, operation_id=operation_id,
                expected_revision=expected_revision,
            )

    def _publish_locked(self, job_id, title, events, *, operation_id="", expected_revision=""):
        state = self._read(job_id)
        operation_id = str(operation_id or "").strip()
        if state.get("state") == "published":
            if operation_id and operation_id == state.get("publish_operation_id"):
                return {"ok": True, "video_id": state.get("video_id"), "replayed": True,
                        "timeline": state.get("published_timeline")}
            raise ValueError("job_already_published")
        if state.get("state") != "review":
            raise ValueError("job_not_ready_for_review")
        try:
            draft = json.loads((self._dir(job_id) / "draft.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("publish_draft_missing") from None
        revision_draft = json.loads(json.dumps(draft))
        for event in revision_draft.get("events") or []:
            if isinstance(event, dict):
                event.setdefault("review_status", "pending")
                event.setdefault("context_before", "")
                event.setdefault("context_after", "")
                event.setdefault("alternate_product_id", "")
                event.setdefault("alternate_every_ms", 0)
        actual_revision = hashlib.sha256(json.dumps(revision_draft, ensure_ascii=False, sort_keys=True,
                                                    separators=(",", ":")).encode("utf-8")).hexdigest()
        if not expected_revision:
            raise ValueError("publish_expected_revision_required")
        if expected_revision != actual_revision:
            raise ValueError("revision_conflict")
        if not operation_id or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", operation_id):
            raise ValueError("publish_operation_id_required")
        # The browser's submit body is deliberately not an authority for the
        # timeline.  Only the reviewed, revision-checked draft on disk may be
        # published; this closes a stale-tab/tampered-request bypass.
        events = [row for row in revision_draft.get("events") or []
                  if isinstance(row, dict) and row.get("review_status") != "rejected"]
        # The immutable library ID is allocated by the server, never derived
        # from a human title or the current date.
        video_id = "video-" + uuid.uuid4().hex
        destination = VIDEO_DIR / video_id
        if destination.exists():
            raise ValueError("video_id_already_exists")
        catalog = {row["product_id"]: row for row in _catalog_rows(self.catalog_provider())}
        clean_events, used = [], []
        for raw in events if isinstance(events, list) else []:
            at_ms = int(raw.get("at_ms"))
            if at_ms < 0 or at_ms >= int(round(float(state["duration_s"]) * 1000)):
                raise ValueError("event_time_out_of_range")
            product_id = str(raw.get("product_id") or "").strip()
            if product_id and product_id not in catalog:
                raise ValueError("event_product_not_in_saved_catalog")
            clean = {"at_ms": at_ms,
                     "product": f"sku_{product_id}" if product_id else None}
            if product_id:
                used.append(product_id)
            alternate_id = str(raw.get("alternate_product_id") or "").strip()
            if alternate_id:
                if not product_id:
                    raise ValueError("alternate_requires_primary_product")
                if alternate_id not in catalog:
                    raise ValueError("alternate_product_not_in_saved_catalog")
                if alternate_id == product_id:
                    raise ValueError("alternate_product_must_be_different")
                alternate_every_ms = int(raw.get("alternate_every_ms") or 0)
                if alternate_every_ms < 10000:
                    raise ValueError("alternate_interval_too_short")
                clean["alternate_with"] = f"sku_{alternate_id}"
                clean["alternate_every_ms"] = alternate_every_ms
                used.append(alternate_id)
            clean_events.append(clean)
        clean_events.sort(key=lambda row: row["at_ms"])
        if not clean_events:
            raise ValueError("events_required")
        if not used:
            raise ValueError("pinned_product_required")
        if len({row["at_ms"] for row in clean_events}) != len(clean_events):
            raise ValueError("duplicate_event_time")
        # Mốc đầu phải ở 0. Bản cũ NÉM LỖI ở đây, nên duyệt hết mốc rồi vẫn
        # không phát hành được khi đoạn nói đầu tiên bắt đầu sau giây 0 — một
        # ngõ cụt ở đúng bước cuối.
        #
        # Đưa về 0 KHÔNG đổi hành vi ghim: `eventAt()` trong schedule.js trả
        # `events[0]` cho mọi vị trí nhỏ hơn mốc đầu, nên sản phẩm đó vốn đã
        # được ghim từ giây 0. Ghi at_ms = 0 chỉ là ghi đúng thực tế đang chạy.
        da_chuan_hoa = None
        if clean_events[0]["at_ms"] != 0:
            da_chuan_hoa = {"tu_ms": clean_events[0]["at_ms"], "ve_ms": 0,
                            "product": clean_events[0].get("product")}
            clean_events[0]["at_ms"] = 0
        products = {}
        for product_id in dict.fromkeys(used):
            products[f"sku_{product_id}"] = timeline_product_entry(catalog[product_id])
        cycle_ms = int(round(float(state["duration_s"]) * 1000))
        timeline = {
            "schema_version": 1, "video_id": video_id,
            "title": str(title or state.get("title") or video_id).strip()[:200],
            "source_file": state["filename"], "cycle_ms": cycle_ms,
            "repin_every_ms": 20000, "products": products, "events": clean_events,
            "script_contexts": [{"at_ms": row["at_ms"],
                                 "topic": (products[row["product"]]["short_name"]
                                           if row["product"] else "Không ghim"),
                                 "facts": []} for row in clean_events],
        }
        errors = validate_timeline(timeline)
        if errors:
            raise ValueError("invalid_timeline:" + "; ".join(errors))
        stage = VIDEO_DIR / f".{video_id}-{uuid.uuid4().hex}.staging"
        stage.mkdir(parents=True)
        try:
            shutil.copy2(self._dir(job_id) / "fingerprint.npz", stage / "fingerprint.npz")
            transcript = self._dir(job_id) / "transcript.json"
            if transcript.exists():
                shutil.copy2(transcript, stage / "transcript.json")
            _atomic_json(stage / "timeline.json", timeline)
            os.replace(stage, destination)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        self._write(job_id, state="published", phase="complete", video_id=video_id,
                    publish_operation_id=operation_id, published_at=_now(),
                    published_timeline=timeline)
        return {"ok": True, "video_id": video_id, "timeline": timeline,
                "da_chuan_hoa_moc_dau": da_chuan_hoa}
