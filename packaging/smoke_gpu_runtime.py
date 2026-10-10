"""Black-box frozen Whisper CPU/GPU smoke test over a private localhost port.

The caller must supply an existing one-folder KidTE build, a local Whisper
model, and a short PCM16 WAV fixture. All app state and generated media live
under a newly-created work directory. Transcript text is never printed or
written to the evidence report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

READY_TIMEOUT_S = 120
ANALYSIS_TIMEOUT_S = 300
UPLOAD_CHUNK_BYTES = 1024 * 1024
KNOWN_CUDA_DLLS = {"cublas64_12.dll", "cublaslt64_12.dll", "cudnn64_9.dll"}
OUTPUT_NAME = "gpu-runtime-smoke-evidence.json"
_LOCAL_HTTP = build_opener(ProxyHandler({}))


class SmokeFailure(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _required_file(path: Path, code: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        raise SmokeFailure(code) from None
    if not resolved.is_file():
        raise SmokeFailure(code)
    return resolved


def _model_config(model_dir: Path) -> Path:
    try:
        model_dir = model_dir.expanduser().resolve(strict=True)
    except OSError:
        raise SmokeFailure("model_directory_missing") from None
    required = ("config.json", "model.bin", "tokenizer.json")
    if not model_dir.is_dir() or any(not (model_dir / name).is_file() for name in required):
        raise SmokeFailure("model_directory_incomplete")
    if not any((model_dir / name).is_file() for name in ("vocabulary.txt", "vocabulary.json")):
        raise SmokeFailure("model_vocabulary_missing")
    return model_dir / "config.json"


def _audio_duration(audio: Path) -> float:
    try:
        with wave.open(str(audio), "rb") as source:
            if source.getcomptype() != "NONE" or source.getsampwidth() != 2:
                raise SmokeFailure("audio_must_be_pcm16_wav")
            rate = source.getframerate()
            duration = source.getnframes() / rate if rate else 0
    except (OSError, wave.Error):
        raise SmokeFailure("audio_wav_invalid") from None
    if duration <= 1.0:
        raise SmokeFailure("audio_fixture_too_short")
    if audio.stat().st_size > 512 * 1024 * 1024:
        raise SmokeFailure("audio_fixture_too_large")
    return duration


def _free_loopback_port() -> int:
    for _attempt in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port != 8793:
            return port
    raise SmokeFailure("could_not_allocate_test_port")


def _child_environment(runtime_root: Path, args) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        normalized = key.upper()
        if normalized.startswith(("KID_TE_", "LIVE_RE_", "KIDTE_GPU_RUNTIME_")):
            env.pop(key, None)
        elif any(token in normalized for token in ("_KEY", "TOKEN", "SECRET", "PASSWORD")) or normalized in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
            env[key] = ""
    sandbox = runtime_root.parent
    local_appdata = sandbox / "localappdata"
    temp_dir = sandbox / "temp"
    hf_cache = sandbox / "hf-cache"
    for path in (local_appdata, temp_dir, hf_cache):
        path.mkdir(parents=True, exist_ok=True)
    env.update({
        "KID_TE_ROOT": str(runtime_root),
        "CHAT_HOST": "127.0.0.1",
        "CHAT_PORT": str(args.port),
        "DEEPSEEK_API_KEY": "",
        "OPENAI_API_KEY": "",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HOME": str(hf_cache),
        "HUGGINGFACE_HUB_CACHE": str(hf_cache),
        "TRANSFORMERS_CACHE": str(hf_cache),
        "LOCALAPPDATA": str(local_appdata),
        "APPDATA": str(sandbox / "appdata"),
        "TEMP": str(temp_dir),
        "TMP": str(temp_dir),
        "TMPDIR": str(temp_dir),
    })
    if args.gpu_root:
        env["KIDTE_GPU_RUNTIME_ROOT"] = str(args.gpu_root)
        env["KIDTE_GPU_RUNTIME_SHA256"] = args.gpu_manifest_sha
    return env


def _http(base: str, path: str, *, method="GET", payload=None, body=None,
          content_type="application/json", timeout=15):
    headers = {"Accept": "application/json"}
    data = body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif body is not None:
        headers["Content-Type"] = content_type
    request = Request(base + path, data=data, headers=headers, method=method)
    try:
        with _LOCAL_HTTP.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, raw
    except HTTPError as exc:
        # Never echo server response bodies: a job response can contain speech.
        raise SmokeFailure(f"http_status_{exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise SmokeFailure("localhost_request_failed") from None


def _json_request(base: str, path: str, *, method="GET", payload=None,
                  body=None, content_type="application/json", timeout=15):
    status, raw = _http(base, path, method=method, payload=payload, body=body,
                        content_type=content_type, timeout=timeout)
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeFailure("localhost_json_invalid") from None
    if not isinstance(result, dict):
        raise SmokeFailure("localhost_json_shape_invalid")
    if status < 200 or status >= 300 or result.get("ok") is False:
        raise SmokeFailure("localhost_api_rejected")
    return result


def _read_stdout(stream, ready_event: threading.Event, marker: str):
    for line in iter(stream.readline, ""):
        if marker in line:
            ready_event.set()
    try:
        stream.close()
    except OSError:
        pass


def _wait_ready(process, ready_event, base, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(f"app_exited_before_ready_{process.returncode}")
        if ready_event.wait(0.2):
            break
    else:
        raise SmokeFailure("app_ready_timeout")
    # The READY marker is emitted only after this exact child bound its port.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(f"app_exited_after_ready_{process.returncode}")
        try:
            status, _ = _http(base, "/health", timeout=2)
            if status == 200:
                return
        except SmokeFailure:
            time.sleep(0.2)
    raise SmokeFailure("app_health_timeout")


def _make_video(app_dir: Path, audio: Path, output: Path) -> float:
    ffmpeg = app_dir / "tools" / "ffmpeg.exe"
    ffprobe = app_dir / "tools" / "ffprobe.exe"
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise SmokeFailure("app_media_tools_missing")
    duration = min(15.0, _audio_duration(audio))
    command = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:r=24",
        "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
        "-t", f"{duration:.3f}", "-shortest", "-c:v", "mpeg4",
        "-q:v", "5", "-pix_fmt", "yuv420p", "-c:a", "aac", str(output),
    ]
    try:
        subprocess.run(command, check=True, timeout=90, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        raise SmokeFailure("ffmpeg_fixture_generation_failed") from None
    if not output.is_file() or output.stat().st_size <= 0:
        raise SmokeFailure("ffmpeg_fixture_missing")
    probe = [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(output)]
    try:
        result = subprocess.run(probe, check=True, timeout=20, capture_output=True, text=True)
        actual_duration = float(json.loads(result.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise SmokeFailure("ffprobe_fixture_failed") from None
    if actual_duration <= 1.0 or actual_duration > 15.05:
        raise SmokeFailure("generated_video_duration_invalid")
    return actual_duration


def _source_probe(video: Path) -> str:
    size = video.stat().st_size
    with video.open("rb") as handle:
        head = handle.read(min(1024 * 1024, size))
        handle.seek(max(0, size - 1024 * 1024))
        tail = handle.read()
    return hashlib.sha256(head + tail + str(size).encode("ascii")).hexdigest()


def _upload_video(base: str, video: Path) -> tuple[str, float]:
    started = time.monotonic()
    size = video.stat().st_size
    job = _json_request(base, "/api/video/upload/start", method="POST", payload={
        "filename": "gpu-runtime-smoke.mp4",
        "total_size": size,
        "source_probe": _source_probe(video),
    })
    job_id = str(job.get("job_id") or "")
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise SmokeFailure("upload_job_id_invalid")
    offset = 0
    with video.open("rb") as handle:
        while offset < size:
            block = handle.read(min(UPLOAD_CHUNK_BYTES, size - offset))
            if not block:
                raise SmokeFailure("upload_fixture_read_failed")
            result = _json_request(
                base,
                f"/api/video/upload/chunk?{urlencode({'job_id': job_id, 'offset': offset})}",
                method="POST", body=block, content_type="application/octet-stream",
            )
            uploaded = result.get("uploaded_size")
            if not isinstance(uploaded, int) or uploaded != offset + len(block):
                raise SmokeFailure("upload_offset_unexpected")
            offset = uploaded
    _json_request(base, "/api/video/upload/finish", method="POST", payload={
        "job_id": job_id, "title": "GPU runtime smoke fixture",
    })
    return job_id, round(time.monotonic() - started, 3)


def _wait_for_review(base: str, job_id: str, process, timeout_s=ANALYSIS_TIMEOUT_S):
    deadline = time.monotonic() + timeout_s
    query = urlencode({"job_id": job_id, "include_draft": "1"})
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(f"app_exited_during_analysis_{process.returncode}")
        result = _json_request(base, f"/api/video/analysis/status?{query}", timeout=15)
        state = result.get("state")
        if state == "review":
            return result
        if state == "error":
            raise SmokeFailure("video_analysis_failed")
        time.sleep(2)
    raise SmokeFailure("video_analysis_timeout")


def _bundled_cuda_files(app_dir: Path) -> list[str]:
    found = []
    for root in (app_dir / "_internal" / "nvidia", app_dir / "nvidia"):
        if not root.is_dir():
            continue
        for path in root.rglob("*.dll"):
            if path.name.casefold() in KNOWN_CUDA_DLLS:
                found.append(path.relative_to(app_dir).as_posix())
    return sorted(found)


def _gpu_fallback_code(log_path: Path) -> str | None:
    if not log_path.is_file():
        return None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") != "whisper_gpu_fallback":
            continue
        code = re.search(r"gpu_runtime_[a-z0-9_]+", str(record.get("message") or ""))
        if code:
            return code.group(0)
    return None


def _validate_args(args):
    if args.expected_device == "cuda" and not (args.gpu_root and args.gpu_manifest_sha):
        raise SmokeFailure("cuda_smoke_requires_gpu_pack_and_manifest_sha")
    if bool(args.gpu_root) != bool(args.gpu_manifest_sha):
        raise SmokeFailure("gpu_root_and_manifest_sha_must_be_paired")
    if args.expect_gpu_rejection:
        if args.expected_device != "cpu" or not args.gpu_root:
            raise SmokeFailure("gpu_rejection_smoke_requires_cpu_and_gpu_pack_args")
    elif args.expected_device == "cpu" and args.gpu_root:
        raise SmokeFailure("cpu_smoke_must_run_without_gpu_pack_env")
    if args.gpu_manifest_sha and not re.fullmatch(r"[0-9a-fA-F]{64}", args.gpu_manifest_sha):
        raise SmokeFailure("gpu_manifest_sha_invalid")
    if args.gpu_root and not args.gpu_root.is_absolute():
        raise SmokeFailure("gpu_root_must_be_absolute")


def run_smoke(args) -> dict:
    _validate_args(args)
    app_dir = args.app_dir.expanduser().resolve(strict=True)
    executable = _required_file(app_dir / "KidTE.exe", "kidte_exe_missing")
    model_dir = args.model.expanduser().resolve(strict=True)
    model_config = _model_config(model_dir)
    audio = _required_file(args.audio, "audio_fixture_missing")
    _audio_duration(audio)
    work_dir = args.work_dir.expanduser().resolve()
    if work_dir.exists():
        raise SmokeFailure("work_dir_must_be_new")
    for source in (app_dir, model_dir, audio):
        if _within(source, work_dir) or _within(work_dir, source):
            raise SmokeFailure("work_dir_must_be_independent")
    work_dir.mkdir(parents=True, exist_ok=False)
    runtime_root = work_dir / "runtime"
    runtime_root.mkdir()
    data_dir = runtime_root / "data"
    machine_dir = data_dir / "machine"
    machine_dir.mkdir(parents=True)
    selection_path = machine_dir / "whisper.json"
    selection = {"custom_path": str(model_dir)}
    selection_path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    (data_dir / "live_products_current.json").write_text(json.dumps({
        "schema_version": 1,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "room_id": "smoke-test-only",
        "product_count": 1,
        "products": [{"product_id": "smoke-product", "title": "Fixture product"}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    video = work_dir / "fixture.mp4"
    video_duration = _make_video(app_dir, audio, video)
    port = _free_loopback_port()
    args.port = port
    base = f"http://127.0.0.1:{port}"
    expected_marker = f"READY http://127.0.0.1:{port}"
    env = _child_environment(runtime_root, args)

    manifest_path = Path(args.gpu_root).expanduser().resolve() / "gpu-runtime.json" if args.gpu_root else None
    supplied_manifest_hash = args.gpu_manifest_sha or ""
    observed_manifest_hash = None
    if manifest_path and manifest_path.is_file():
        observed_manifest_hash = _sha256_file(manifest_path)
        if not args.expect_gpu_rejection and observed_manifest_hash.casefold() != supplied_manifest_hash.casefold():
            raise SmokeFailure("gpu_manifest_sha_mismatch_before_launch")
    elif args.expected_device == "cuda":
        raise SmokeFailure("gpu_manifest_missing")

    evidence = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "expected_device": args.expected_device,
        "app": {"path": str(executable), "sha256": _sha256_file(executable)},
        "model": {"path": str(model_dir), "config_path": str(model_config),
                  "config_sha256": _sha256_file(model_config)},
        "audio": {"path": str(audio), "sha256": _sha256_file(audio),
                  "bytes": audio.stat().st_size},
        "gpu_pack": ({"root": str(Path(args.gpu_root).expanduser().resolve()),
                      "manifest_path": str(manifest_path),
                      "supplied_manifest_sha256": supplied_manifest_hash,
                      "observed_manifest_sha256": observed_manifest_hash,
                      "expect_rejection": bool(args.expect_gpu_rejection)}
                     if args.gpu_root else None),
        "server": {"host": "127.0.0.1", "port": port},
        "sandbox": {"work_dir": str(work_dir), "runtime_root": str(runtime_root)},
        "fixture": {"video_path": str(video), "video_bytes": video.stat().st_size,
                    "video_duration_s": video_duration},
    }

    process = None
    reader = None
    ready = threading.Event()
    failure = None
    try:
        process = subprocess.Popen(
            [str(executable), "--safe-mode", "--no-ui", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=str(app_dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        reader = threading.Thread(target=_read_stdout, args=(process.stdout, ready, expected_marker),
                                  daemon=True, name="kidte-smoke-output")
        reader.start()
        startup_started = time.monotonic()
        _wait_ready(process, ready, base, READY_TIMEOUT_S)
        evidence["timing"] = {"startup_s": round(time.monotonic() - startup_started, 3)}
        for route in ("/health", "/app", "/assets/kid-te-icon.png"):
            status, _body = _http(base, route, timeout=15)
            if status != 200:
                raise SmokeFailure(f"http_probe_failed_{route.strip('/').replace('/', '_')}")
        evidence["http_probes"] = {"health": 200, "app": 200, "static_asset": 200}

        job_id, upload_s = _upload_video(base, video)
        analysis_started = time.monotonic()
        status = _wait_for_review(base, job_id, process)
        analysis_s = round(time.monotonic() - analysis_started, 3)
        draft = status.get("draft")
        transcription = draft.get("transcription") if isinstance(draft, dict) else None
        if not isinstance(transcription, dict):
            raise SmokeFailure("transcription_metadata_missing")
        actual_device = str(transcription.get("device") or "")
        if actual_device != args.expected_device:
            raise SmokeFailure("transcription_device_mismatch")

        transcript_path = runtime_root / "data" / "video_uploads" / job_id / "transcript.json"
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SmokeFailure("transcript_artifact_missing") from None
        segments = transcript.get("segments")
        if not isinstance(segments, list) or not segments:
            raise SmokeFailure("transcript_segments_empty")
        retained_selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if retained_selection != selection:
            raise SmokeFailure("selected_model_not_retained")
        required_artifacts = (selection_path, transcript_path, runtime_root / "logs" / "core.jsonl")
        if any(not _within(path, runtime_root) for path in required_artifacts):
            raise SmokeFailure("runtime_artifact_root_escape")
        fallback_code = _gpu_fallback_code(runtime_root / "logs" / "core.jsonl")
        if args.expect_gpu_rejection and not fallback_code:
            raise SmokeFailure("invalid_gpu_pack_rejection_unobserved")

        bundled_cuda = _bundled_cuda_files(app_dir)
        if bundled_cuda:
            raise SmokeFailure("core_contains_bundled_cuda_dlls")
        evidence.update({
            "status": "passed",
            "job": {"job_id": job_id, "state": status.get("state"),
                    "device": actual_device, "compute_type": transcription.get("compute_type"),
                    "model": str(transcription.get("model") or ""),
                    "transcript_segment_count": len(segments),
                    "model_selection_retained": True,
                    "analysis_artifact_dir": str(transcript_path.parent)},
            "gpu_fallback_code": fallback_code,
            "bundled_cuda_dlls": bundled_cuda,
            "artifacts_in_sandbox": True,
            "timing": {"startup_s": evidence["timing"]["startup_s"],
                       "upload_s": upload_s, "analysis_s": analysis_s,
                       "total_s": round(time.monotonic() - startup_started, 3)},
        })
    except SmokeFailure as exc:
        failure = exc.code
    except Exception as exc:  # noqa: BLE001 - always record failure and stop the owned child
        # Keep diagnostics stable and do not echo subprocess/API bodies.
        failure = f"unexpected_{type(exc).__name__}"
    finally:
        if process is not None and process.poll() is None:
            try:
                _json_request(base, "/api/shutdown", method="POST", payload={}, timeout=3)
            except SmokeFailure:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate()  # Exact child returned by Popen; never kill by name.
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        if reader is not None:
            reader.join(timeout=2)
        if process is not None:
            evidence["process"] = {"pid": process.pid, "return_code": process.poll()}
        if failure:
            evidence["status"] = "failed"
            evidence["failure_code"] = failure
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        evidence_path = Path(args.work_dir).expanduser().resolve() / OUTPUT_NAME
        try:
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            evidence["evidence_path"] = str(evidence_path)
        except OSError:
            if not failure:
                failure = "evidence_write_failed"
    if failure:
        evidence["status"] = "failed"
        evidence["failure_code"] = failure
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True,
                        help="Frozen one-folder directory containing KidTE.exe and tools/.")
    parser.add_argument("--model", type=Path, required=True,
                        help="Existing local faster-whisper model directory.")
    parser.add_argument("--audio", type=Path, required=True,
                        help="Existing PCM16 WAV fixture with speech.")
    parser.add_argument("--work-dir", type=Path, required=True,
                        help="New, non-existing isolated output directory.")
    parser.add_argument("--expected-device", choices=("cpu", "cuda"), required=True)
    parser.add_argument("--gpu-root", type=Path,
                        help="Versioned external GPU pack root, for CUDA or rejection smoke.")
    parser.add_argument("--gpu-manifest-sha", default="",
                        help="SHA256 of the exact gpu-runtime.json manifest bytes.")
    parser.add_argument("--expect-gpu-rejection", action="store_true",
                        help="Pass a deliberately invalid GPU selection and require the logged CPU fallback.")
    args = parser.parse_args(argv)
    try:
        evidence = run_smoke(args)
    except SmokeFailure as exc:
        print(f"GPU runtime smoke failed: {exc.code}", file=sys.stderr)
        return 2
    evidence_path = Path(args.work_dir).expanduser().resolve() / OUTPUT_NAME
    if evidence.get("status") != "passed":
        print(f"GPU runtime smoke failed: {evidence.get('failure_code', 'unknown')}", file=sys.stderr)
        print(f"Evidence: {evidence_path}", file=sys.stderr)
        return 1
    print(f"GPU runtime smoke passed; evidence: {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
