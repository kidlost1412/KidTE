"""Bounded JSON client for the separately packaged transcription runtime."""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from pathlib import Path

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_SEGMENTS = 100_000
MAX_TEXT_BYTES = 8 * 1024 * 1024
PROTOCOL = 1


def _absolute_file(value, field):
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise ValueError(f"transcription_{field}_path_must_be_absolute")
    if not path.is_file():
        raise ValueError(f"transcription_{field}_missing")
    return str(path.resolve())


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _validate_response(raw):
    if not isinstance(raw, dict) or raw.get("protocol") != PROTOCOL:
        raise RuntimeError("transcription_worker_protocol_invalid")
    if raw.get("ok") is not True:
        if set(raw) != {"protocol", "ok", "error"}:
            raise RuntimeError("transcription_worker_result_invalid")
        code = raw.get("error")
        if not isinstance(code, str) or not code or len(code) > 120:
            code = "worker_failed"
        raise RuntimeError(f"transcription_worker:{code}")
    if set(raw) != {"protocol", "ok", "segments", "info"}:
        raise RuntimeError("transcription_worker_result_invalid")
    segments = raw.get("segments")
    info = raw.get("info")
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise RuntimeError("transcription_worker_segments_invalid")
    if not isinstance(info, dict) or set(info) != {
        "language", "language_probability", "device", "compute_type", "model",
    }:
        raise RuntimeError("transcription_worker_info_invalid")

    rows = []
    text_size = 0
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != {"start", "end", "text"}:
            raise RuntimeError("transcription_worker_segment_invalid")
        start, end, text = segment["start"], segment["end"], segment["text"]
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or end > 7 * 24 * 60 * 60
                or not isinstance(text, str) or len(text) > 20_000):
            raise RuntimeError("transcription_worker_segment_invalid")
        text_size += len(text.encode("utf-8"))
        if text_size > MAX_TEXT_BYTES:
            raise RuntimeError("transcription_worker_text_too_large")
        rows.append({"start": round(float(start), 3), "end": round(float(end), 3),
                     "text": text.strip()})

    language = info["language"]
    probability = info["language_probability"]
    if (not isinstance(language, str) or len(language) > 32
            or isinstance(probability, bool) or not isinstance(probability, (int, float))
            or not math.isfinite(probability) or not 0 <= probability <= 1
            or info["device"] not in {"cpu", "cuda"}
            or not isinstance(info["compute_type"], str) or len(info["compute_type"]) > 40
            or not isinstance(info["model"], str) or len(info["model"]) > 4096):
        raise RuntimeError("transcription_worker_info_invalid")
    clean_info = {
        "language": language,
        "language_probability": round(float(probability), 4),
        "device": info["device"], "compute_type": info["compute_type"],
        "model": info["model"],
    }
    return rows, clean_info


def _run_worker(worker, request, env, args=(), timeout=6 * 60 * 60):
    """Drain both pipes concurrently while retaining bounded output only."""
    process = subprocess.Popen(
        [str(worker), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # F7/F8: BELOW_NORMAL + Job Object KILL_ON_JOB_CLOSE, theo dõi để dừng khi thoát.
    import resource_share
    resource_share.adopt(process)
    try:
        return _collect(process, request, timeout)
    finally:
        resource_share.release(process)


def _collect(process, request, timeout):
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    oversized = threading.Event()

    def drain(name, stream, limit):
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            room = max(0, limit + 1 - len(captured[name]))
            if room:
                captured[name].extend(chunk[:room])
            if name == "stdout" and len(captured[name]) > limit:
                oversized.set()
                try:
                    process.kill()
                except OSError:
                    pass

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout, MAX_RESPONSE_BYTES),
                         daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr, MAX_STDERR_BYTES),
                         daemon=True),
    ]
    for thread in readers:
        thread.start()
    try:
        try:
            process.stdin.write(request)
            process.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("transcription_worker_timeout")
    finally:
        for thread in readers:
            thread.join(timeout=10)
        for stream in (process.stdout, process.stderr):
            stream.close()
    if oversized.is_set():
        raise RuntimeError("transcription_worker_result_too_large")
    return return_code, bytes(captured["stdout"])


def transcribe(audio_path, model_path, cpu_threads=None):
    """Run the local transcription worker and validate its complete reply."""
    if not model_path:
        raise RuntimeError("Máy chưa có model chép lời — vào Cài đặt → Chép lời để tải model")
    wav = _absolute_file(audio_path, "audio")
    model = Path(str(model_path))
    if not model.is_absolute() or not model.is_dir():
        raise RuntimeError("transcription_model_path_invalid")
    try:
        from component_runtime import component_path
        worker = component_path("video-analysis-cpu", "KidTETranscription.exe")
    except (ImportError, OSError, ValueError) as exc:
        raise RuntimeError("transcription_worker_package_unavailable") from exc
    if not worker:
        raise RuntimeError("transcription_worker_package_unavailable")
    worker = Path(worker)
    if not worker.is_file():
        raise RuntimeError("transcription_worker_package_unavailable")

    request = json.dumps({
        "protocol": PROTOCOL, "audio_wav": wav,
        "model_path": str(model.resolve()),
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        raise RuntimeError("transcription_worker_request_too_large")
    env = os.environ.copy()
    # Ensure the subprocess receives the exact selected GPU pack and the same
    # absolute data root as the core process.
    from config import ROOT
    env["KID_TE_ROOT"] = str(Path(ROOT).resolve())
    # Truyền qua biến môi trường (không đổi giao thức JSON nghiêm ngặt):
    # worker cũ bỏ qua, worker mới đưa vào WhisperModel(cpu_threads=...).
    env.pop("KID_TE_CPU_THREADS", None)
    if cpu_threads:
        env["KID_TE_CPU_THREADS"] = str(int(cpu_threads))
    try:
        return_code, stdout = _run_worker(worker, request, env)
    except OSError as exc:
        raise RuntimeError("transcription_worker_start_failed") from exc
    if return_code != 0:
        raise RuntimeError(f"transcription_worker_exit:{return_code}")
    try:
        payload = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("transcription_worker_protocol_invalid") from exc
    return _validate_response(payload)


def gpu_available():
    """Return whether the installed worker can initialize the selected GPU pack."""
    try:
        from component_runtime import component_path
        worker = component_path("video-analysis-cpu", "KidTETranscription.exe")
    except (ImportError, OSError, ValueError):
        return False
    if not worker or not Path(worker).is_file():
        return False
    env = os.environ.copy()
    from config import ROOT
    env["KID_TE_ROOT"] = str(Path(ROOT).resolve())
    try:
        return_code, stdout = _run_worker(
            Path(worker), b"", env, ("--probe-gpu",), timeout=30,
        )
        if return_code != 0 or len(stdout) > MAX_RESPONSE_BYTES:
            return False
        payload = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
        return (isinstance(payload, dict) and set(payload) == {
            "protocol", "ok", "gpu_available",
        } and payload["protocol"] == PROTOCOL and payload["ok"] is True
                and payload["gpu_available"] is True)
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
