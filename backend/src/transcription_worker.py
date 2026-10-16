"""Standalone entry point for local faster-whisper transcription.

The worker accepts one protocol-1 JSON request on stdin and writes exactly one
JSON response to stdout. It never selects, downloads, or opens application data.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import sys
import wave
from pathlib import Path

PROTOCOL = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_AUDIO_BYTES = 24 * 1024**3
MAX_SEGMENTS = 100_000
MAX_TEXT_BYTES = 8 * 1024 * 1024
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
_GPU_DLL_HANDLES = []


def _read_request(stream):
    raw = stream.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("request_too_large")
    try:
        request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request_invalid_json") from exc
    if (not isinstance(request, dict)
            or set(request) != {"protocol", "audio_wav", "model_path"}
            or request.get("protocol") != PROTOCOL):
        raise ValueError("request_protocol_invalid")
    for key in ("audio_wav", "model_path"):
        value = request.get(key)
        if not isinstance(value, str) or not value or len(value) > 32768:
            raise ValueError("request_path_invalid")
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("request_path_must_be_absolute")
    return request


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("request_duplicate_json_key")
        value[key] = item
    return value


def _read_pcm_wav(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("audio_wav_missing_or_too_large")
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or wav.getframerate() != 16000 or wav.getcomptype() != "NONE"):
                raise ValueError("audio_wav_format_invalid")
            frames = wav.getnframes()
            if frames <= 0 or frames > (7 * 24 * 60 * 60 * 16000):
                raise ValueError("audio_wav_duration_invalid")
            raw = wav.readframes(frames)
    except (OSError, wave.Error) as exc:
        raise ValueError("audio_wav_invalid") from exc
    if len(raw) != frames * 2:
        raise ValueError("audio_wav_truncated")
    import numpy as np
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _validate_model(path):
    model = Path(path)
    if not model.is_dir() or not all((model / name).is_file() for name in MODEL_FILES):
        raise ValueError("local_model_missing_or_invalid")
    if not ((model / "vocabulary.txt").is_file()
            or (model / "vocabulary.json").is_file()):
        raise ValueError("local_model_missing_or_invalid")
    return str(model.resolve())


def _env_cpu_threads():
    """Giới hạn luồng CPU do lõi đặt khi đang LIVE (KID_TE_CPU_THREADS)."""
    try:
        value = int(os.environ.get("KID_TE_CPU_THREADS") or 0)
    except ValueError:
        return 0
    return value if 1 <= value <= 256 else 0


def _transcribe(request):
    model_path = _validate_model(request["model_path"])
    audio = _read_pcm_wav(request["audio_wav"])
    import gpu_runtime

    gpu_error = None
    try:
        gpu_runtime.register_cuda_dll_dirs()
    except gpu_runtime.GpuRuntimeError:
        # The explicitly selected pack is invalid; keep the CPU path usable.
        gpu_error = True

    from faster_whisper import WhisperModel

    last_error = None
    devices = (("cpu", "int8"),) if gpu_error else (
        ("cuda", "int8_float16"), ("cpu", "int8"),
    )
    for device, compute in devices:
        try:
            extra = {}
            threads = _env_cpu_threads()
            if threads and device == "cpu":
                extra["cpu_threads"] = threads
            model = WhisperModel(
                model_path, device=device, compute_type=compute,
                local_files_only=True, **extra,
            )
            segments, info = model.transcribe(
                audio, language="vi", vad_filter=True, beam_size=5,
                condition_on_previous_text=True,
            )
            rows = []
            text_size = 0
            for item in segments:
                start, end = float(item.start), float(item.end)
                text = str(item.text or "").strip()
                if (not math.isfinite(start) or not math.isfinite(end)
                        or start < 0 or end < start or end > 7 * 24 * 60 * 60
                        or len(text) > 20_000):
                    raise ValueError("worker_segment_invalid")
                text_size += len(text.encode("utf-8"))
                if len(rows) >= MAX_SEGMENTS or text_size > MAX_TEXT_BYTES:
                    raise ValueError("worker_result_too_large")
                rows.append({"start": round(start, 3), "end": round(end, 3),
                             "text": text})
            probability = float(info.language_probability)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                probability = 0.0
            return {
                "protocol": PROTOCOL, "ok": True, "segments": rows,
                "info": {"language": str(info.language or "")[:32],
                         "language_probability": round(probability, 4),
                         "device": device, "compute_type": compute,
                         "model": model_path},
            }
        except Exception as exc:  # noqa: BLE001 - native GPU/CPU backends raise vendor-specific errors.
            last_error = exc
            if device == "cuda":
                continue
    # The response error is a stable code; details and filesystem contents stay
    # on stderr/logs rather than expanding the machine-readable protocol.
    error_name = type(last_error).__name__ if last_error else "WorkerError"
    return {"protocol": PROTOCOL, "ok": False,
            "error": f"whisper_failed:{error_name}"[:120]}


def _probe():
    """Check packaged imports only; never touch settings or model downloads."""
    try:
        import ctranslate2  # noqa: F401
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - report missing or broken native package imports.
        return {"protocol": PROTOCOL, "ok": False,
                "error": f"worker_package_invalid:{type(exc).__name__}"[:120]}
    return {"protocol": PROTOCOL, "ok": True, "capability": "local-transcription"}


def _probe_gpu():
    """Check that the selected CUDA runtime is usable by CTranslate2."""
    try:
        import gpu_runtime
        gpu_runtime.register_cuda_dll_dirs()
        if os.name == "nt":
            _GPU_DLL_HANDLES.extend((
                ctypes.WinDLL("cublas64_12.dll"),
                ctypes.WinDLL("cudnn64_9.dll"),
            ))
        import ctranslate2
        available = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - CUDA discovery may fail through platform-specific DLL errors.
        available = False
    return {"protocol": PROTOCOL, "ok": True, "gpu_available": bool(available)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--probe"]:
        result = _probe()
    elif argv == ["--probe-gpu"]:
        result = _probe_gpu()
    elif argv:
        result = {"protocol": PROTOCOL, "ok": False, "error": "arguments_invalid"}
    else:
        try:
            result = _transcribe(_read_request(sys.stdin))
        except Exception as exc:  # noqa: BLE001 - preserve the one-response protocol for any worker failure.
            code = str(exc) if isinstance(exc, ValueError) else f"worker_error:{type(exc).__name__}"
            result = {"protocol": PROTOCOL, "ok": False, "error": code[:120]}
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
