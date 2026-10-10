import hashlib
import json
import sys
import types
import wave
from pathlib import Path

import gpu_runtime
import numpy as np
import pytest
import video_analysis

_DLLS = {
    "nvidia/cublas/bin/cublas64_12.dll": b"cublas",
    "nvidia/cublas/bin/cublasLt64_12.dll": b"cublasLt",
    "nvidia/cudnn/bin/cudnn64_9.dll": b"cudnn",
    "nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll": b"nvrtc",
}
_LICENSES = {
    "licenses/nvidia-cublas-cu12/LICENSE": b"license cublas",
    "licenses/nvidia-cuda-nvrtc-cu12/LICENSE": b"license nvrtc",
    "licenses/nvidia-cudnn-cu12/LICENSE": b"license cudnn",
}
_DISTRIBUTIONS = [
    {"name": "nvidia-cublas-cu12", "version": "12.4.5.8", "record_sha256": "1" * 64},
    {"name": "nvidia-cuda-nvrtc-cu12", "version": "12.4.127", "record_sha256": "2" * 64},
    {"name": "nvidia-cudnn-cu12", "version": "9.1.0.70", "record_sha256": "3" * 64},
]


def _write_pack(root, *, changes=None):
    root.mkdir(parents=True)
    contents = {**_DLLS, **_LICENSES}
    for relative, payload in contents.items():
        path = root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "component": "gpu-cuda12",
        "version": root.name,
        "platform": "windows",
        "architecture": "x86_64",
        "runtime": "cuda12",
        "python_abi": "cp311",
        "ctranslate2_version": "4.6.0",
        "cudnn_major": 9,
        "dll_directories": sorted({str(Path(path).parent).replace("\\", "/")
                                   for path in _DLLS}),
        "files": [{
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        } for relative, payload in sorted(contents.items())],
        "provenance": {
            "source_commit": "a" * 40,
            "distributions": _DISTRIBUTIONS,
            "license_metadata": [{
                "path": relative,
                "distribution": relative.split("/")[1],
                "source_path": relative.split("/")[1] + ".dist-info/licenses/LICENSE",
            } for relative in sorted(_LICENSES)],
        },
    }
    if changes:
        changes(manifest)
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (root / gpu_runtime.MANIFEST_NAME).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _mock_windows_runtime(monkeypatch, *, registered=None):
    registered = registered if registered is not None else []
    monkeypatch.setattr(gpu_runtime.os, "name", "nt")
    monkeypatch.setattr(gpu_runtime, "sys", types.SimpleNamespace(
        platform="win32", version_info=(3, 11, 9),
        implementation=types.SimpleNamespace(name="cpython", cache_tag="cpython-311"),
        executable=sys.executable,
    ))
    monkeypatch.setattr(gpu_runtime.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(gpu_runtime.struct, "calcsize", lambda _format: 8)
    monkeypatch.setattr(gpu_runtime.importlib.metadata, "version", lambda _name: "4.6.0")
    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory",
                        lambda path: registered.append(("dir", path)) or _FakeHandle())
    monkeypatch.setattr(gpu_runtime.ctypes, "WinDLL",
                        lambda path: registered.append(("dll", path)) or _FakeHandle())
    gpu_runtime._CUDA_DLL_HANDLES.clear()
    gpu_runtime._LAST_CONFIG = None
    gpu_runtime._LAST_ERROR = None
    gpu_runtime._RUNTIME_MODE = None
    return registered


class _FakeHandle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _select_pack(monkeypatch, root, digest):
    monkeypatch.setenv(gpu_runtime.ROOT_ENV, str(root))
    monkeypatch.setenv(gpu_runtime.SHA256_ENV, digest)


def test_valid_pack_is_verified_then_registered_and_handles_are_retained(tmp_path, monkeypatch):
    root = tmp_path / "gpu-cuda12" / "1.0.0"
    digest = _write_pack(root)
    registered = _mock_windows_runtime(monkeypatch)
    _select_pack(monkeypatch, root, digest)
    original_path = "C:\\Windows\\System32"
    monkeypatch.setenv("PATH", original_path)

    assert video_analysis._register_cuda_dll_dirs() is True
    assert [kind for kind, _path in registered].count("dir") == 3
    assert [kind for kind, _path in registered].count("dll") == 3
    assert len(video_analysis._CUDA_DLL_HANDLES) == 6
    assert os_path_prefix(monkeypatch) == original_path
    first_count = len(registered)

    assert video_analysis._register_cuda_dll_dirs() is True
    assert len(registered) == first_count

    monkeypatch.setenv(gpu_runtime.ROOT_ENV, str(root.parent / "other-version"))
    with pytest.raises(gpu_runtime.GpuRuntimeError, match="configuration_changed_restart_required"):
        video_analysis._register_cuda_dll_dirs()
    assert len(registered) == first_count


def os_path_prefix(monkeypatch):
    path = gpu_runtime.os.environ["PATH"]
    return path.split(gpu_runtime.os.pathsep, 3)[-1]


@pytest.mark.parametrize("failure", ["bad_digest", "bad_abi", "bad_ctranslate", "bad_json"])
def test_invalid_manifest_is_rejected_before_any_dll_registration(
        tmp_path, monkeypatch, failure):
    root = tmp_path / "1.0.0"
    if failure == "bad_abi":
        digest = _write_pack(root, changes=lambda manifest: manifest.update(python_abi="cp312"))
    elif failure == "bad_ctranslate":
        digest = _write_pack(root, changes=lambda manifest: manifest.update(ctranslate2_version="4.5.0"))
    elif failure == "bad_json":
        root.mkdir(parents=True)
        (root / gpu_runtime.MANIFEST_NAME).write_bytes(b"{broken")
        digest = hashlib.sha256(b"{broken").hexdigest()
    else:
        digest = _write_pack(root)
    registered = _mock_windows_runtime(monkeypatch)
    _select_pack(monkeypatch, root, "0" * 64 if failure == "bad_digest" else digest)
    before_path = "untouched-path"
    monkeypatch.setenv("PATH", before_path)

    with pytest.raises(gpu_runtime.GpuRuntimeError):
        video_analysis._register_cuda_dll_dirs()

    assert registered == []
    assert gpu_runtime.os.environ["PATH"] == before_path
    assert video_analysis._CUDA_DLL_HANDLES == []


@pytest.mark.parametrize("tamper", ["content", "extra_file"])
def test_payload_mismatch_is_rejected_before_registration(tmp_path, monkeypatch, tamper):
    root = tmp_path / "1.0.0"
    digest = _write_pack(root)
    if tamper == "content":
        (root / "nvidia/cublas/bin/cublas64_12.dll").write_bytes(b"changed")
    else:
        (root / "nvidia/cublas/bin/unlisted.dll").write_bytes(b"extra")
    registered = _mock_windows_runtime(monkeypatch)
    _select_pack(monkeypatch, root, digest)

    with pytest.raises(gpu_runtime.GpuRuntimeError):
        video_analysis._register_cuda_dll_dirs()

    assert registered == []


def test_partial_registration_requires_restart_before_selecting_another_pack(tmp_path, monkeypatch):
    root = tmp_path / "1.0.0"
    digest = _write_pack(root)
    _mock_windows_runtime(monkeypatch)
    created_handles = []
    attempts = []

    def add_directory(path):
        attempts.append(path)
        if len(attempts) == 2:
            raise OSError("injected registration failure")
        handle = _FakeHandle()
        created_handles.append(handle)
        return handle

    monkeypatch.setattr(gpu_runtime.os, "add_dll_directory", add_directory)
    _select_pack(monkeypatch, root, digest)
    original_path = "unchanged"
    monkeypatch.setenv("PATH", original_path)

    with pytest.raises(gpu_runtime.GpuRuntimeError, match="registration_failed"):
        video_analysis._register_cuda_dll_dirs()
    assert created_handles[0].closed is True
    assert gpu_runtime._CUDA_DLL_HANDLES == []
    assert gpu_runtime.os.environ["PATH"] == original_path

    monkeypatch.delenv(gpu_runtime.ROOT_ENV)
    monkeypatch.delenv(gpu_runtime.SHA256_ENV)
    with pytest.raises(gpu_runtime.GpuRuntimeError, match="configuration_changed_restart_required"):
        video_analysis._register_cuda_dll_dirs()


def test_invalid_explicit_pack_forces_transcription_to_cpu(tmp_path, monkeypatch):
    wav = tmp_path / "speech.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(np.zeros(1600, dtype="<i2").tobytes())
    _mock_windows_runtime(monkeypatch)
    monkeypatch.setenv(gpu_runtime.ROOT_ENV, str(tmp_path / "missing-pack"))
    monkeypatch.setenv(gpu_runtime.SHA256_ENV, "a" * 64)
    monkeypatch.setattr(video_analysis, "_find_whisper_model", lambda: "fake-model")
    devices = []

    class FakeModel:
        def __init__(self, _model, *, device, **_kwargs):
            devices.append(device)

        def transcribe(self, _audio, **_kwargs):
            info = types.SimpleNamespace(language="vi", language_probability=0.99)
            return [types.SimpleNamespace(start=0, end=0.1, text=" thử")], info

    problems = types.SimpleNamespace(report_problem=lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setitem(sys.modules, "problems", problems)

    rows, metadata = video_analysis._transcribe(wav)

    assert devices == ["cpu"]
    assert metadata["device"] == "cpu"
    assert rows[0]["text"] == "thử"


def test_invalid_explicit_pack_does_not_bypass_whisper_readiness_check(monkeypatch, tmp_path):
    _mock_windows_runtime(monkeypatch)
    monkeypatch.setenv(gpu_runtime.ROOT_ENV, str(tmp_path / "missing-pack"))
    monkeypatch.setenv(gpu_runtime.SHA256_ENV, "a" * 64)
    called = []
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(
        get_cuda_device_count=lambda: called.append(True) or 1,
    ))
    import whisper_models

    assert whisper_models._cuda_ready() is False
    assert called == []
