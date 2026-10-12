# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder build for the core and service plugins."""
from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)


PROJECT = Path(SPECPATH).parent
BACKEND = PROJECT / "backend"
PLUGINS = PROJECT / "plugins"
sys.path.insert(0, str(PROJECT / "packaging"))
from stage_runtime import build_version, runtime_version

APP_VERSION = build_version()
# The runtime package has its own slow-moving version (its major is the runtime ABI);
# keeping it out of the exe resources keeps the runtime bytes stable across releases.
RUNTIME_VERSION = runtime_version()
version_parts = [int(part) for part in RUNTIME_VERSION.split(".")]
if len(version_parts) != 3:
    raise ValueError(f"Unexpected runtime version: {RUNTIME_VERSION}")
# PE numeric version fields use 16-bit components; the displayed string keeps
# the complete release number even when a date-based fallback exceeds 65535.
PE_VERSION = tuple(min(part, 65535) for part in version_parts) + (0,)

BUILD_PROFILE = os.environ.get("KIDTE_BUILD_PROFILE", "cpu").strip().lower()
if BUILD_PROFILE not in {"cpu", "gpu"}:
    raise SystemExit("KIDTE_BUILD_PROFILE must be 'cpu' or 'gpu'.")


def _is_nvidia_binary(item):
    """Recognize CUDA libraries in PyInstaller TOCs and collect_all tuples."""
    if len(item) >= 3:  # Analysis TOC: (destination, source, typecode)
        destination, source = item[0], item[1]
    else:  # collect_all: (source, destination)
        source, destination = item[0], item[1]
    normalized = str(source).replace("\\", "/").lower()
    target = str(destination).replace("\\", "/").lower()
    basename = Path(str(source)).name.lower()
    return (
        "/nvidia/" in normalized
        or target.startswith("nvidia/")
        or "/nvidia/" in target
        or basename.startswith(("cublas", "cudnn", "nvrtc", "nvjitlink"))
    )


def _filter_nvidia_entries(entries):
    """Filter NVIDIA-owned entries in CPU mode, preserving entries in GPU mode."""
    if BUILD_PROFILE == "cpu":
        return [item for item in entries if not _is_nvidia_binary(item)]
    return list(entries)


def _apply_build_profile(analysis_result):
    """Remove hook-discovered CUDA DLLs in CPU mode from analysis results."""
    analysis_result.binaries = _filter_nvidia_entries(analysis_result.binaries)
    analysis_result.datas = _filter_nvidia_entries(analysis_result.datas)
    return analysis_result


def windows_version_info(description, version=None):
    version = version or APP_VERSION
    pe_version = tuple(min(int(part), 65535) for part in version.split(".")) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=pe_version, prodvers=pe_version),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "Kid TE"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", description.replace(" ", "")),
                StringStruct("OriginalFilename", description.replace(" ", "") + ".exe"),
                StringStruct("ProductName", "Kid TE"),
                StringStruct("ProductVersion", version),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

webview_datas, webview_binaries, webview_hidden = collect_all("webview")
openai_datas, openai_binaries, openai_hidden = collect_all("openai")
# Chỉ lấy cacert.pem; collect_all kéo cả certifi/tests (stage_runtime cấm file test).
certifi_datas, certifi_binaries, certifi_hidden = collect_data_files("certifi"), [], []
truststore_datas, truststore_binaries, truststore_hidden = collect_all("truststore")
httpx_datas, httpx_binaries, httpx_hidden = collect_all("httpx")
whisper_datas, whisper_binaries, whisper_hidden = [], [], []
ctranslate_datas, ctranslate_binaries, ctranslate_hidden = [], [], []
common_pathex = [str(BACKEND / "src")]

# Legacy GPU profile bundles CUDA DLLs. The default CPU profile leaves NVIDIA
# packages out; filtering below also catches PyInstaller hook auto-collection.
import site
cuda_binaries = []
for packages in site.getsitepackages():
    nvidia = Path(packages) / "nvidia"
    for pkg in ("cublas", "cudnn", "cuda_nvrtc"):
        for dll in sorted((nvidia / pkg / "bin").glob("*.dll")):
            cuda_binaries.append((str(dll), f"nvidia/{pkg}/bin"))
if BUILD_PROFILE == "gpu" and not any("cublas64_12" in src.lower() for src, _ in cuda_binaries):
    raise SystemExit("GPU profile requested but cuBLAS is missing; install backend/requirements-gpu.txt.")

# Kid TE's own resources (prompts, web UI) belong to the separately updated ``app``
# package (stage_runtime.stage), not to the runtime bundle.
core_datas = []
# Khong dong goi du lieu nao cua shop (ho so san pham, thong tin shop, anh xa):
# may moi cai la may trang. Khop runtime_layout.SEED_PATHS (rong) — xem
# test_packaging_policy.py.
def analysis(script, *, pathex=None, datas=None, hiddenimports=None):
    return Analysis(
        [str(script)],
        pathex=common_pathex + list(pathex or []),
        binaries=(webview_binaries + openai_binaries + certifi_binaries + truststore_binaries + httpx_binaries + whisper_binaries
                  + ctranslate_binaries + cuda_binaries) if script.name == "app_entry.py" else [],
        datas=(core_datas + webview_datas + openai_datas + certifi_datas + truststore_datas + httpx_datas + whisper_datas
               + ctranslate_datas) if script.name == "app_entry.py" else list(datas or []),
        hiddenimports=(webview_hidden + openai_hidden + certifi_hidden + truststore_hidden + httpx_hidden
                       + ["net", "truststore", "certifi", "httpx", "httpcore", "h11", "anyio"] + whisper_hidden
                       + ctranslate_hidden + ["webview.platforms.edgechromium", "backup_crypto",
                                             "backup_v2", "backup_inventory", "backup_secret_stage", "backup_service", "backup_io",
                           "cryptography.hazmat.primitives.ciphers.aead", "cryptography.hazmat.primitives.kdf.scrypt"]
                       if script.name == "app_entry.py" else list(hiddenimports or [])),
        hookspath=[], hooksconfig={}, runtime_hooks=[],
        excludes=["faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers", "torch", "nvidia"], noarchive=False,
    )


# The full analysis of app_entry.py discovers every third-party dependency; Kid TE's
# own modules are then dropped from the archive because the runtime loads them from
# the app package (see packaging/runtime_bootstrap.py).
core = _apply_build_profile(analysis(BACKEND / "src" / "app_entry.py"))
_OWN_SOURCE = os.path.normcase(os.path.abspath(str(BACKEND / "src")))


def _is_own_module(entry):
    source = os.path.normcase(os.path.abspath(str(entry[1])))
    return source == _OWN_SOURCE or source.startswith(_OWN_SOURCE + os.sep)


own_modules = sorted(entry[0] for entry in core.pure if _is_own_module(entry))
core.pure = [entry for entry in core.pure if not _is_own_module(entry)]
print(f"runtime: {len(own_modules)} Kid TE modules moved to the app package")
bootstrap = Analysis([str(PROJECT / "packaging" / "runtime_bootstrap.py")], pathex=[], binaries=[],
                     datas=[], hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
                     excludes=[], noarchive=False)
video = _apply_build_profile(analysis(PLUGINS / "dong-bo-video" / "chay.py",
                 pathex=[str(PLUGINS / "dong-bo-video")], hiddenimports=["websocket"]))

core_pyz = PYZ(core.pure)
video_pyz = PYZ(video.pure)

core_exe = EXE(core_pyz, bootstrap.scripts, [], exclude_binaries=True,
               name="KidTE", console=False, debug=False, bootloader_ignore_signals=False,
               strip=False, upx=True, disable_windowed_traceback=False,
               version=windows_version_info("Kid TE", RUNTIME_VERSION),
               icon=str(PROJECT / "assets" / "kid-te.ico"))
video_exe = EXE(video_pyz, video.scripts, [], exclude_binaries=True,
                name="KidTEVideo", console=True, debug=False,
                bootloader_ignore_signals=False, strip=False, upx=True,
                disable_windowed_traceback=False,
                version=windows_version_info("Kid TE Video"),
                icon=str(PROJECT / "assets" / "kid-te.ico"))

# Filter once more at the shared COLLECT boundary so neither analysis can
# contribute NVIDIA DLLs to the final CPU one-folder package.
collect_binaries = _filter_nvidia_entries(core.binaries)

dist = COLLECT(
    core_exe,
    collect_binaries, core.datas,
    strip=False, upx=True, upx_exclude=["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll"],
    name="KidTE",
)
video_dist = COLLECT(video_exe, video.binaries, video.datas,
                     strip=False, upx=True, name="KidTEVideo")
