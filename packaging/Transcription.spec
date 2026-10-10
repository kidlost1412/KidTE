# -*- mode: python ; coding: utf-8 -*-  # noqa: UP009 - PyInstaller spec convention.
"""Independent one-folder CPU transcription worker executable."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

PROJECT = Path(SPECPATH).parent  # noqa: F821 - injected by PyInstaller.
BACKEND = PROJECT / "backend"
SRC = BACKEND / "src"


def _nvidia_entry(item):
    source = str(item[0]).replace("\\", "/").lower()
    destination = str(item[1]).replace("\\", "/").lower()
    basename = Path(str(item[0])).name.lower()
    return ("/nvidia/" in source or destination.startswith("nvidia/")
            or "/nvidia/" in destination
            or basename.startswith(("cublas", "cudnn", "nvrtc", "nvjitlink")))


def _test_entry(item):
    path = str(item[0]).replace("\\", "/").lower()
    parts = path.split("/")
    name = parts[-1]
    return (any(part in {"test", "tests", "testing"} for part in parts)
            or name.startswith("test_") or name.endswith("_test.py"))


def _nvidia_toc(item):
    destination, source = str(item[0]), str(item[1])
    return _nvidia_entry((source, destination))


def _test_toc(item):
    return _test_entry((item[1], item[0]))


def _test_module(item):
    name = str(item[0]).lower().replace("/", ".").replace("\\", ".")
    return any(part in {"test", "tests", "testing"} for part in name.split("."))


datas, binaries, hiddenimports = [], [], []
for package in ("faster_whisper", "ctranslate2", "numpy", "av", "onnxruntime"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += [item for item in package_datas if not _test_entry(item)]
    binaries += [item for item in package_binaries if not _test_entry(item)]
    hiddenimports += [item for item in package_hidden
                      if not any(part in {"test", "tests", "testing"}
                                 for part in item.lower().split("."))]
# Hugging Face metadata is read locally by faster-whisper. The worker is
# configured with local_files_only and never downloads a model.
for package in ("huggingface-hub", "tokenizers", "tqdm"):
    datas += copy_metadata(package)

# This artifact is always CPU-only. Optional CUDA DLLs are installed separately
# as the validated external GPU component and discovered through gpu_runtime.
binaries = [item for item in binaries
            if not _nvidia_entry(item) and not _test_entry(item)]
datas = [item for item in datas
         if not _nvidia_entry(item) and not _test_entry(item)]

analysis = Analysis(  # noqa: F821 - injected by PyInstaller.
    [str(SRC / "transcription_worker.py")],
    pathex=[str(SRC)], binaries=binaries, datas=datas,
    hiddenimports=list(dict.fromkeys(hiddenimports + ["gpu_runtime"])),
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["openai", "webview", "numpy.tests", "numpy._core.tests",
              "numpy.f2py", "numpy.distutils", "setuptools", "pkg_resources",
              "server", "component_store", "config_settings"],
    noarchive=False,
)
# Apply the same exclusions to hook-discovered TOCs; these hooks run after the
# explicit package allowlist above.
analysis.binaries = [item for item in analysis.binaries
                     if not _nvidia_toc(item) and not _test_toc(item)]
analysis.datas = [item for item in analysis.datas
                  if not _nvidia_toc(item) and not _test_toc(item)]
analysis.pure = [item for item in analysis.pure if not _test_module(item)]
pyz = PYZ(analysis.pure)  # noqa: F821 - injected by PyInstaller.
exe = EXE(  # noqa: F821 - injected by PyInstaller.
    pyz, analysis.scripts, [], exclude_binaries=True,
    # The process protocol uses redirected stdin/stdout; CREATE_NO_WINDOW is
    # set by the core when it launches this console executable.
    name="KidTETranscription", console=True, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False,
    disable_windowed_traceback=False,
)
dist = COLLECT(exe, analysis.binaries, analysis.datas,  # noqa: F821 - injected by PyInstaller.
               strip=False, upx=False, name="KidTETranscription")
