import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT / "packaging" / "KidTE.spec"
BUILD_PATH = PROJECT / "packaging" / "build.ps1"


def _nvidia_filter(profile):
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    wanted = {"_is_nvidia_binary", "_filter_nvidia_entries"}
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {"Path": Path, "BUILD_PROFILE": profile}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SPEC_PATH), "exec"), namespace)  # noqa: S102
    return namespace["_filter_nvidia_entries"]


def test_spec_profile_defaults_to_cpu_and_validates_profile_values():
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "BUILD_PROFILE"
                              for target in node.targets))
    environment_read = next(
        node for node in ast.walk(assignment)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
    )
    assert ast.literal_eval(environment_read.args[1]) == "cpu"
    allowed_values = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name) and node.left.id == "BUILD_PROFILE"
        and isinstance(node.ops[0], ast.NotIn)
    )
    assert ast.literal_eval(allowed_values.comparators[0]) == {"cpu", "gpu"}


def test_cpu_profile_filters_nvidia_from_real_pyinstaller_analysis_tocs():
    # PyInstaller Analysis binaries/datas use (destination, source, typecode).
    binaries = [
        ("nvidia/cublas/bin/cublas64_12.dll", r"C:\venv\Lib\site-packages\nvidia\cublas\bin\cublas64_12.dll", "BINARY"),
        ("nvidia/cudnn/bin/cudnn64_9.dll", r"C:\venv\Lib\site-packages\nvidia\cudnn\bin\cudnn64_9.dll", "BINARY"),
        ("nvidia/cuda_nvrtc/bin/nvrtc64_120_0.dll", r"C:\venv\Lib\site-packages\nvidia\cuda_nvrtc\bin\nvrtc64_120_0.dll", "BINARY"),
        ("ctranslate2/ctranslate2.dll", r"C:\venv\Lib\site-packages\ctranslate2\ctranslate2.dll", "BINARY"),
    ]
    datas = [
        ("nvidia/cudnn/LICENSE.txt", r"C:\venv\Lib\site-packages\nvidia\cudnn\LICENSE.txt", "DATA"),
        ("backend/prompts/system_prompt.txt", r"D:\KidTE\backend\prompts\system_prompt.txt", "DATA"),
    ]

    filtered_binaries = _nvidia_filter("cpu")(binaries)
    filtered_datas = _nvidia_filter("cpu")(datas)

    assert filtered_binaries == [binaries[-1]]
    assert filtered_datas == [datas[-1]]


def test_gpu_profile_preserves_nvidia_entries_in_analysis_tocs():
    tocs = [
        ("nvidia/cublas/bin/cublas64_12.dll", r"C:\venv\Lib\site-packages\nvidia\cublas\bin\cublas64_12.dll", "BINARY"),
        ("ctranslate2/ctranslate2.dll", r"C:\venv\Lib\site-packages\ctranslate2\ctranslate2.dll", "BINARY"),
    ]
    assert _nvidia_filter("gpu")(tocs) == tocs


def test_filter_also_handles_collect_all_source_destination_pairs():
    entries = [
        (r"C:\venv\Lib\site-packages\nvidia\cublas\bin\cublas64_12.dll", "nvidia/cublas/bin"),
        (r"C:\venv\Lib\site-packages\ctranslate2\ctranslate2.dll", "ctranslate2"),
    ]
    assert _nvidia_filter("cpu")(entries) == [entries[-1]]


def test_analysis_profile_applies_to_shared_core_and_sidecar_collection():
    # Model the shared COLLECT input from both Analysis objects.
    core = SimpleNamespace(binaries=[
        ("nvidia/cublas/bin/cublas64_12.dll", r"C:\venv\nvidia\cublas64_12.dll", "BINARY"),
        ("core/core.dll", r"C:\venv\core.dll", "BINARY"),
    ])
    sidecar = SimpleNamespace(binaries=[
        ("nvidia/cudnn/bin/cudnn64_9.dll", r"C:\venv\nvidia\cudnn64_9.dll", "BINARY"),
        ("video/video.dll", r"C:\venv\video.dll", "BINARY"),
    ])
    collected = _nvidia_filter("cpu")(core.binaries + sidecar.binaries)
    assert collected == [core.binaries[-1], sidecar.binaries[-1]]


def test_cpu_and_gpu_runtime_locks_keep_the_profiles_separate():
    backend = PROJECT / "backend"
    runtime = (backend / "requirements.txt").read_text(encoding="utf-8").lower()
    cpu_lock = (backend / "requirements-cpu.lock.txt").read_text(encoding="utf-8").lower()
    gpu_profile = (backend / "requirements-gpu.txt").read_text(encoding="utf-8").lower()
    gpu_lock = (backend / "requirements-gpu.lock.txt").read_text(encoding="utf-8").lower()
    assert "nvidia-" not in runtime
    assert "nvidia-" not in cpu_lock
    for pin in ("faster-whisper==1.2.1", "ctranslate2==4.8.2", "numpy==2.4.6",
                "onnxruntime==1.30.0", "requests==2.34.2"):
        assert pin in cpu_lock
    for package in ("nvidia-cublas-cu12", "nvidia-cuda-nvrtc-cu12", "nvidia-cudnn-cu12"):
        assert package in gpu_profile
        assert package in gpu_lock


def test_build_script_separates_core_and_audited_captcha_interpreters_and_reports_profile():
    source = BUILD_PATH.read_text(encoding="utf-8")
    assert '[string]$CaptchaPython' in source
    assert '$CaptchaPython = $Python' in source
    assert '& $CaptchaPython -m PyInstaller' in source
    assert '"/DPackageProfile=$ProfileSlug"' in source
    assert 'KidTE-Setup-$Version-$ArtifactProfile.exe' in source
    assert 'KidTE-$Version-$ArtifactProfile.release-manifest.json' in source
    assert 'KidTE-$Version-$ArtifactProfile.inventory.json' in source
    assert '--compressed-artifact $Installer --output $InventoryPath' in source


def test_release_version_survives_history_reset(monkeypatch):
    spec = importlib.util.spec_from_file_location("stage_release_override", PROJECT / "packaging" / "stage_runtime.py")
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    monkeypatch.setenv("KIDTE_RELEASE_VERSION", "1.14.254")
    assert stage.build_version() == "1.14.254"
    monkeypatch.setenv("KIDTE_RELEASE_VERSION", "../1.14.254")
    with pytest.raises(ValueError, match="three-part"):
        stage.build_version()
