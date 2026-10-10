"""Resolve the optional, operator-selected CUDA 12 runtime for Whisper.

An external runtime is trusted only when its manifest digest is supplied by
the operator and every manifested file matches its recorded size and SHA256.
The legacy PyInstaller/source-venv discovery remains available when the
external-runtime environment variables are absent.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import importlib.metadata
import json
import os
import platform
import re
import struct
import sys
import threading
from pathlib import Path

ROOT_ENV = "KIDTE_GPU_RUNTIME_ROOT"
SHA256_ENV = "KIDTE_GPU_RUNTIME_SHA256"
MANIFEST_NAME = "gpu-runtime.json"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_REQUIRED_DLLS = {
    "nvidia/cublas/bin/cublas64_12.dll",
    "nvidia/cublas/bin/cublasLt64_12.dll",
    "nvidia/cudnn/bin/cudnn64_9.dll",
}
_ALLOWED_DLL_DIRS = {
    "nvidia/cublas/bin",
    "nvidia/cudnn/bin",
    "nvidia/cuda_nvrtc/bin",
}
_MANIFEST_FIELDS = {
    "schema_version", "component", "version", "platform", "architecture",
    "runtime", "python_abi", "ctranslate2_version", "cudnn_major",
    "dll_directories", "files", "provenance",
}
_FILE_FIELDS = {"path", "sha256", "size"}
_CUDA_DLL_HANDLES = []
_LOCK = threading.RLock()
_LAST_CONFIG = None
_LAST_ERROR = None
_RUNTIME_MODE = None


class GpuRuntimeError(RuntimeError):
    """The explicitly selected optional GPU runtime is not trusted/usable."""


def external_runtime_selected():
    """Return whether the operator explicitly selected an external runtime."""
    return ROOT_ENV in os.environ or SHA256_ENV in os.environ


def _duplicate_key_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _fail(code):
    raise GpuRuntimeError(code)


def _config_key():
    return (
        os.environ.get(ROOT_ENV), os.environ.get(SHA256_ENV),
        sys.executable, sys.platform, platform.machine(),
        tuple(sys.version_info[:3]),
    )


def _python_abi():
    if sys.implementation.name != "cpython" or tuple(sys.version_info[:2]) != (3, 11):
        _fail("gpu_runtime_python_abi_unsupported")
    if struct.calcsize("P") != 8 or platform.machine().casefold() not in {"amd64", "x86_64"}:
        _fail("gpu_runtime_architecture_unsupported")
    if getattr(sys.implementation, "cache_tag", "") != "cpython-311":
        _fail("gpu_runtime_python_abi_unsupported")
    return "cp311"


def _inside(root, path):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse_point(path):
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _safe_relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or ":" in value or "\x00" in value):
        _fail("gpu_runtime_manifest_path_invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        _fail("gpu_runtime_manifest_path_invalid")
    if path.as_posix() != value:
        _fail("gpu_runtime_manifest_path_invalid")
    return path


def _validate_provenance(value):
    # Keep provenance in the pinned manifest structurally meaningful.  Its
    # manual trust anchor is KIDTE_GPU_RUNTIME_SHA256, not this metadata.
    if not isinstance(value, dict) or set(value) != {
        "source_commit", "distributions", "license_metadata"
    }:
        _fail("gpu_runtime_provenance_invalid")
    if (not isinstance(value["source_commit"], str)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", value["source_commit"])):
        _fail("gpu_runtime_provenance_invalid")
    distributions = value["distributions"]
    if not isinstance(distributions, list):
        _fail("gpu_runtime_provenance_invalid")
    required_distributions = {
        "nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12", "nvidia-cudnn-cu12",
    }
    last_name = ""
    seen_distributions = set()
    for item in distributions:
        if not isinstance(item, dict) or set(item) != {"name", "version", "record_sha256"}:
            _fail("gpu_runtime_provenance_invalid")
        name, version, record_hash = item["name"], item["version"], item["record_sha256"]
        if (not isinstance(name, str) or not name or name.casefold() <= last_name
                or name not in required_distributions
                or not isinstance(version, str) or not version
                or not isinstance(record_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", record_hash)):
            _fail("gpu_runtime_provenance_invalid")
        expected_major = "9" if name == "nvidia-cudnn-cu12" else "12"
        if version.split(".", 1)[0] != expected_major:
            _fail("gpu_runtime_provenance_invalid")
        last_name = name.casefold()
        seen_distributions.add(name)
    if seen_distributions != required_distributions:
        _fail("gpu_runtime_provenance_invalid")
    licenses = value["license_metadata"]
    if not isinstance(licenses, list):
        _fail("gpu_runtime_provenance_invalid")
    last_path = ""
    license_paths = set()
    for item in licenses:
        if not isinstance(item, dict) or set(item) != {"path", "distribution", "source_path"}:
            _fail("gpu_runtime_provenance_invalid")
        path, distribution, source_path = (
            item["path"], item["distribution"], item["source_path"]
        )
        _safe_relative_path(path)
        if (not path.startswith("licenses/") or path.casefold() <= last_path
                or not isinstance(distribution, str)
                or distribution not in required_distributions
                or not isinstance(source_path, str) or not source_path):
            _fail("gpu_runtime_provenance_invalid")
        _safe_relative_path(source_path)
        last_path = path.casefold()
        license_paths.add(path.casefold())
    if not licenses:
        _fail("gpu_runtime_provenance_invalid")
    return license_paths


def _validate_manifest(root, raw_manifest, expected_digest):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_digest or ""):
        _fail("gpu_runtime_manifest_digest_required")
    actual_digest = hashlib.sha256(raw_manifest).hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest.casefold()):
        _fail("gpu_runtime_manifest_digest_mismatch")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"), object_pairs_hook=_duplicate_key_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _fail("gpu_runtime_manifest_invalid")
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        _fail("gpu_runtime_manifest_schema_invalid")
    if (manifest["schema_version"] != 1 or manifest["component"] != "gpu-cuda12"
            or manifest["platform"] != "windows" or manifest["architecture"] != "x86_64"
            or manifest["runtime"] != "cuda12" or manifest["python_abi"] != _python_abi()
            or manifest["cudnn_major"] != 9):
        _fail("gpu_runtime_abi_incompatible")
    version = manifest["version"]
    if not isinstance(version, str) or not version or root.name != version:
        _fail("gpu_runtime_version_invalid")
    ctranslate_version = manifest["ctranslate2_version"]
    if not isinstance(ctranslate_version, str) or not ctranslate_version:
        _fail("gpu_runtime_manifest_schema_invalid")
    try:
        installed_ctranslate_version = importlib.metadata.version("ctranslate2")
    except importlib.metadata.PackageNotFoundError:
        _fail("gpu_runtime_ctranslate2_metadata_missing")
    if installed_ctranslate_version != ctranslate_version:
        _fail("gpu_runtime_ctranslate2_incompatible")
    license_paths = _validate_provenance(manifest["provenance"])

    files = manifest["files"]
    if not isinstance(files, list) or not files:
        _fail("gpu_runtime_manifest_files_invalid")
    entries = {}
    previous_path = ""
    folded_paths = set()
    dll_dirs = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
            _fail("gpu_runtime_manifest_files_invalid")
        relative = item["path"]
        relative_path = _safe_relative_path(relative)
        folded_path = relative.casefold()
        if folded_path <= previous_path or folded_path in folded_paths:
            _fail("gpu_runtime_manifest_files_unsorted")
        previous_path = folded_path
        folded_paths.add(folded_path)
        if (not isinstance(item["size"], int) or isinstance(item["size"], bool)
                or item["size"] < 0 or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            _fail("gpu_runtime_manifest_files_invalid")
        if relative.startswith("licenses/"):
            pass
        elif relative.startswith("nvidia/") and relative.endswith(".dll"):
            parent = relative_path.parent.as_posix()
            if parent not in _ALLOWED_DLL_DIRS:
                _fail("gpu_runtime_manifest_file_not_allowed")
            dll_dirs.add(parent)
        else:
            _fail("gpu_runtime_manifest_file_not_allowed")
        entries[relative] = item
    if not _REQUIRED_DLLS.issubset(entries):
        _fail("gpu_runtime_required_dll_missing")
    if {path.casefold() for path in entries if path.startswith("licenses/")} != license_paths:
        _fail("gpu_runtime_provenance_invalid")
    directories = manifest["dll_directories"]
    if (not isinstance(directories, list) or directories != sorted(set(directories))
            or set(directories) != dll_dirs or not dll_dirs):
        _fail("gpu_runtime_manifest_directories_invalid")

    listed_paths = set(entries)
    actual_paths = set()
    for path in root.rglob("*"):
        if _is_reparse_point(path) or path.is_symlink():
            _fail("gpu_runtime_reparse_point_not_allowed")
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        if not _inside(root, resolved):
            _fail("gpu_runtime_path_escape")
        relative = path.relative_to(root).as_posix()
        if relative != MANIFEST_NAME:
            actual_paths.add(relative)
    if actual_paths != listed_paths:
        _fail("gpu_runtime_manifest_file_set_mismatch")

    for relative, item in entries.items():
        target = root.joinpath(*_safe_relative_path(relative).parts)
        if _is_reparse_point(target) or target.is_symlink():
            _fail("gpu_runtime_reparse_point_not_allowed")
        resolved = target.resolve(strict=True)
        if not _inside(root, resolved) or not target.is_file():
            _fail("gpu_runtime_path_escape")
        try:
            if target.stat().st_size != item["size"]:
                _fail("gpu_runtime_file_size_mismatch")
            digest = hashlib.sha256()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            _fail("gpu_runtime_file_unreadable")
        if not hmac.compare_digest(digest.hexdigest(), item["sha256"]):
            _fail("gpu_runtime_file_digest_mismatch")
    return [root.joinpath(*Path(item).parts) for item in directories]


def _register_external(root_text, digest_text):
    global _RUNTIME_MODE
    candidate = Path(root_text)
    if (not candidate.is_absolute() or candidate.is_symlink()
            or _is_reparse_point(candidate)):
        _fail("gpu_runtime_root_must_be_absolute")
    try:
        root = candidate.resolve(strict=True)
    except OSError:
        _fail("gpu_runtime_root_missing")
    if not root.is_dir():
        _fail("gpu_runtime_root_missing")
    manifest_path = root / MANIFEST_NAME
    if _is_reparse_point(manifest_path) or manifest_path.is_symlink():
        _fail("gpu_runtime_manifest_path_invalid")
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            _fail("gpu_runtime_manifest_too_large")
        raw_manifest = manifest_path.read_bytes()
    except OSError:
        _fail("gpu_runtime_manifest_missing")
    dll_dirs = _validate_manifest(root, raw_manifest, digest_text)

    # No directory is registered and PATH is untouched until the complete
    # package, manifest digest, ABI and file inventory have passed validation.
    new_handles = []
    try:
        for directory in dll_dirs:
            new_handles.append(os.add_dll_directory(str(directory)))
            # From this point a failed load may leave a native DLL mapped in
            # the process; changing to another runtime would require restart.
            _RUNTIME_MODE = "external"
        if not new_handles:
            _fail("gpu_runtime_dll_directories_missing")
        dll_paths = {path.relative_to(root).as_posix(): path for path in root.rglob("*.dll")}
        for relative in (
            "nvidia/cublas/bin/cublasLt64_12.dll",
            "nvidia/cublas/bin/cublas64_12.dll",
            "nvidia/cudnn/bin/cudnn64_9.dll",
        ):
            new_handles.append(ctypes.WinDLL(str(dll_paths[relative])))
    except Exception as exc:
        for handle in reversed(new_handles):
            try:
                handle.close()
            except Exception:  # noqa: BLE001, S110 - the caller reports the original registration error
                pass
        if isinstance(exc, GpuRuntimeError):
            raise
        _fail("gpu_runtime_dll_registration_failed")
    _CUDA_DLL_HANDLES.extend(new_handles)
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([*(str(path) for path in dll_dirs), current_path])
    return True


def _legacy_directories(frozen_root=None, executable=None):
    frozen_root = Path(frozen_root or getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    executable = Path(executable or sys.executable)
    candidates = [
        frozen_root / "nvidia" / "cublas" / "bin",
        frozen_root / "nvidia" / "cudnn" / "bin",
        frozen_root / "nvidia" / "cuda_nvrtc" / "bin",
    ]
    for parent in executable.resolve().parents:
        packages = parent / "backend" / ".venv" / "Lib" / "site-packages"
        candidates.extend([
            packages / "nvidia" / "cublas" / "bin",
            packages / "nvidia" / "cudnn" / "bin",
            packages / "nvidia" / "cuda_nvrtc" / "bin",
        ])
    return candidates


def register_cuda_dll_dirs(frozen_root=None, executable=None):
    """Register external GPU pack or the established bundled/source runtime."""
    global _LAST_CONFIG, _LAST_ERROR, _RUNTIME_MODE
    if os.name != "nt":
        return False
    with _LOCK:
        selected_external = external_runtime_selected()
        key = _config_key() if selected_external else None
        if _RUNTIME_MODE == "external":
            if not selected_external or key != _LAST_CONFIG:
                _fail("gpu_runtime_configuration_changed_restart_required")
            if _LAST_ERROR is not None:
                raise _LAST_ERROR
            return bool(_CUDA_DLL_HANDLES)
        if _RUNTIME_MODE == "legacy" and selected_external:
            _fail("gpu_runtime_configuration_changed_restart_required")
        if selected_external:
            if key == _LAST_CONFIG:
                if _LAST_ERROR is not None:
                    raise _LAST_ERROR
                return bool(_CUDA_DLL_HANDLES)
            _LAST_CONFIG, _LAST_ERROR = key, None
            try:
                result = _register_external(os.environ.get(ROOT_ENV, ""), os.environ.get(SHA256_ENV, ""))
            except GpuRuntimeError as exc:
                _LAST_ERROR = exc
                raise
            except Exception as exc:
                _LAST_ERROR = GpuRuntimeError("gpu_runtime_validation_failed")
                raise _LAST_ERROR from exc
            if result:
                _RUNTIME_MODE = "external"
            return result

        if _CUDA_DLL_HANDLES:
            return True
        available = []
        for path in _legacy_directories(frozen_root=frozen_root, executable=executable):
            if path.is_dir():
                available.append(path)
                try:
                    # Windows removes the directory when this handle is GC'd.
                    _CUDA_DLL_HANDLES.append(os.add_dll_directory(str(path)))
                except (OSError, AttributeError):
                    continue
        if available:
            # Keep the established behavior for this packaged/source runtime;
            # it is needed by some CTranslate2 Windows builds at first encode.
            current = os.environ.get("PATH", "")
            os.environ["PATH"] = os.pathsep.join(
                [str(path) for path in available] + [current]
            )
            for filename in ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll"):
                for path in available:
                    dll = path / filename
                    if not dll.is_file():
                        continue
                    try:
                        _CUDA_DLL_HANDLES.append(ctypes.WinDLL(str(dll)))
                    except OSError:
                        pass
                    break
            _RUNTIME_MODE = "legacy"
        return bool(_CUDA_DLL_HANDLES)
