import csv
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
BASELINE_GPU_DLLS = {
    "nvidia/cublas/bin": (
        "cublas64_12.dll", "cublasLt64_12.dll", "nvblas64_12.dll",
    ),
    "nvidia/cudnn/bin": (
        "cudnn64_9.dll", "cudnn_adv64_9.dll", "cudnn_cnn64_9.dll",
        "cudnn_engines_precompiled64_9.dll", "cudnn_engines_runtime_compiled64_9.dll",
        "cudnn_engines_tensor_ir64_9.dll", "cudnn_ext64_9.dll", "cudnn_graph64_9.dll",
        "cudnn_heuristic64_9.dll", "cudnn_ops64_9.dll",
    ),
    "nvidia/cuda_nvrtc/bin": (
        "nvrtc-builtins64_129.dll", "nvrtc64_120_0.alt.dll", "nvrtc64_120_0.dll",
    ),
}


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, PROJECT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stage = _load("component_stage_runtime", "packaging/stage_runtime.py")
inventory_module = _load("component_inventory", "packaging/component_inventory.py")
gpu_pack = _load("gpu_pack", "packaging/gpu_pack.py")


def _dist_info(site, normalized, name, version, license_text):
    info = site / f"{normalized}-{version}.dist-info"
    license_file = info / "licenses" / "LICENSE.txt"
    license_file.parent.mkdir(parents=True, exist_ok=True)
    license_file.write_text(license_text, encoding="utf-8")
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        encoding="utf-8")
    record = info / "RECORD"
    relative_license = license_file.relative_to(site).as_posix()
    with record.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([relative_license, "", ""])
        writer.writerow([record.relative_to(site).as_posix(), "", ""])
    return info


def _gpu_site_packages(root):
    site = root / "site-packages"
    for directory, names in BASELINE_GPU_DLLS.items():
        for name in names:
            path = site / directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{directory}/{name}".encode())
    for dist_name, name, version in (
        ("nvidia_cublas_cu12", "nvidia-cublas-cu12", "12.1.0"),
        ("nvidia_cudnn_cu12", "nvidia-cudnn-cu12", "9.1.0"),
        ("nvidia_cuda_nvrtc_cu12", "nvidia-cuda-nvrtc-cu12", "12.1.0"),
        ("ctranslate2", "ctranslate2", "4.8.2"),
    ):
        _dist_info(site, dist_name, name, version, f"License for {name}\n")
    return site


def test_inventory_reports_measured_ownership_bytes_and_optional_archive(tmp_path):
    root = tmp_path / "dist"
    (root / "tools").mkdir(parents=True)
    (root / "KidTE.exe").write_bytes(b"12345")
    (root / "tools" / "ffmpeg.exe").write_bytes(b"1234567")
    (root / "Captcha" / "_internal" / "captcha-monitor").mkdir(parents=True)
    (root / "Captcha" / "_internal" / "captcha-monitor" / "monitor.ps1").write_bytes(b"12")
    archive = tmp_path / "setup.exe"
    archive.write_bytes(b"compressed-bytes")

    report = inventory_module.inventory(root, archive)
    totals = {row["component"]: row["raw_bytes"] for row in report["components"]}
    assert totals == {"runtime": 5, "media-tools": 7, "captcha-monitor": 2}
    assert report["total_raw_bytes"] == 14
    assert report["compressed_artifact"] == {
        "filename": "setup.exe", "bytes": len(b"compressed-bytes"),
        "sha256": hashlib.sha256(b"compressed-bytes").hexdigest(),
    }
    assert sum(row["size"] for row in report["files"]) == 14
    assert all(len(row["sha256"]) == 64 for row in report["files"])


def test_cpu_profile_keeps_runtime_assets_but_rejects_gpu_payload_and_user_weights(tmp_path):
    root = stage._bo_khung_hop_le(tmp_path) if hasattr(stage, "_bo_khung_hop_le") else tmp_path
    # Build the minimal staged layout from the existing policy test helper.
    if root == tmp_path:
        for relative in ("KidTE.exe", "KidTEVideo.exe", "VERSION", "tools/ffmpeg.exe",
                         "tools/ffprobe.exe", "extension/manifest.json",
                         "extension-shopee/manifest.json"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        for plugin_id, names in stage.PLUGIN_FILES.items():
            for name in names:
                path = root / "plugins" / plugin_id / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
        for folder, names in (("extension", stage.EXTENSION_FILES),
                              ("extension-shopee", stage.SHOPEE_EXTENSION_FILES),
                              ("docs", stage.DOC_FILES)):
            for name in names:
                path = root / folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
        for relative in (
            "Captcha/KidTECaptcha.exe",
            "Captcha/_internal/tiktok_live_studio_captcha_solver/resources/modern_puzzle_slider.png",
            "Captcha/_internal/captcha-monitor/monitor.ps1",
            "Captcha/_internal/captcha-monitor/record-and-send.ps1",
            "backend/src/app_entry.py",
            "backend/src/web/operator.html",
            "backend/src/web/kid-te-icon.png",
            "backend/src/web/app/bootstrap.js",
            "backend/src/web/styles/tokens.css",
            "backend/prompts/system_prompt.txt",
            "backend/prompts/sales_playbook.default.json",
            "backend/prompts/knowledge_seed.json",
            "_internal/certifi/cacert.pem",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    # Existing test helper's fixture is copied locally because tests remain isolated.
    for relative in (
        "_internal/nvidia/cublas/bin/cublas64_12.dll",
        "_internal/nvidia/cudnn/bin/cudnn64_9.dll",
    ):
        (root / relative).unlink(missing_ok=True)
    library = root / "_internal/ctranslate2/models/model.py"
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_text("# library code", encoding="utf-8")
    vad = root / "_internal/faster_whisper/assets/silero_vad_v6.onnx"
    vad.parent.mkdir(parents=True, exist_ok=True)
    vad.write_bytes(b"runtime onnx")

    assert stage.validate(root, profile="cpu")["ok"] is True
    library.unlink()
    # CTranslate2 modules may live in PyInstaller's PYZ rather than as loose files.
    assert stage.validate(root, profile="cpu")["ok"] is True

    stray_gpu = root / "_internal/nvidia/cublas/bin/cublas64_12.dll"
    stray_gpu.parent.mkdir(parents=True, exist_ok=True)
    stray_gpu.write_bytes(b"gpu")
    with pytest.raises(RuntimeError, match="forbidden") as error:
        stage.validate(root, profile="cpu")
    assert "_internal/nvidia/cublas/bin/cublas64_12.dll" in str(error.value)
    stray_gpu.unlink()

    misplaced_gpu = root / "Captcha/plugin/nvrtc64_120_0.dll"
    misplaced_gpu.parent.mkdir(parents=True, exist_ok=True)
    misplaced_gpu.write_bytes(b"gpu")
    with pytest.raises(RuntimeError, match="forbidden") as error:
        stage.validate(root, profile="cpu")
    assert "Captcha/plugin/nvrtc64_120_0.dll" in str(error.value)
    misplaced_gpu.unlink()

    user_weight = root / "_internal/backend/models/large/model.bin"
    user_weight.parent.mkdir(parents=True, exist_ok=True)
    user_weight.write_bytes(b"weights")
    with pytest.raises(RuntimeError, match="forbidden"):
        stage.validate(root, profile="cpu")


@pytest.mark.parametrize("relative", [
    "_internal/ctranslate2/models/model.bin",
    "_internal/ctranslate2/models/model.onnx",
    "_internal/backend/DuLieu/model.bin",
])
def test_cpu_profile_rejects_weights_even_in_runtime_package_paths(tmp_path, relative):
    root = tmp_path
    for path in (
        "KidTE.exe", "KidTEVideo.exe", "VERSION", "tools/ffmpeg.exe", "tools/ffprobe.exe",
        "extension/manifest.json", "extension-shopee/manifest.json", "Captcha/KidTECaptcha.exe",
        "Captcha/_internal/tiktok_live_studio_captcha_solver/resources/modern_puzzle_slider.png",
        "Captcha/_internal/captcha-monitor/monitor.ps1",
        "Captcha/_internal/captcha-monitor/record-and-send.ps1",
        "backend/src/app_entry.py", "backend/src/web/operator.html", "backend/src/web/kid-te-icon.png",
        "backend/src/web/app/bootstrap.js", "backend/src/web/styles/tokens.css",
        "backend/prompts/system_prompt.txt", "backend/prompts/sales_playbook.default.json",
        "backend/prompts/knowledge_seed.json", "_internal/certifi/cacert.pem",
        "_internal/ctranslate2/models/model.py",
    ):
        item = root / path
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"x")
    for plugin_id, names in stage.PLUGIN_FILES.items():
        for name in names:
            item = root / "plugins" / plugin_id / name
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"x")
    for folder, names in (("extension", stage.EXTENSION_FILES),
                          ("extension-shopee", stage.SHOPEE_EXTENSION_FILES),
                          ("docs", stage.DOC_FILES)):
        for name in names:
            item = root / folder / name
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_bytes(b"x")
    weight = root / relative
    weight.parent.mkdir(parents=True, exist_ok=True)
    weight.write_bytes(b"weights")
    with pytest.raises(RuntimeError, match="forbidden") as error:
        stage.validate(root, profile="cpu")
    assert relative in str(error.value)


def test_gpu_pack_is_deterministic_and_manifest_covers_every_archive_payload(tmp_path):
    assert gpu_pack.DLL_ALLOWLIST == {
        directory: frozenset(name.casefold() for name in names)
        for directory, names in BASELINE_GPU_DLLS.items()
    }
    site = _gpu_site_packages(tmp_path)
    kwargs = {
        "version": "1.0.0", "python_abi": "cp311",
        "source_commit": "a" * 40,
    }
    first = tmp_path / "one.zip"
    second = tmp_path / "two.zip"
    first_result = gpu_pack.build(site, first, **kwargs)
    second_result = gpu_pack.build(site, second, **kwargs)

    assert first.read_bytes() == second.read_bytes()
    assert first_result["manifest_sha256"] == second_result["manifest_sha256"]
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names, key=str.casefold)
        assert "gpu-runtime.json" in names
        assert all(name.startswith(("nvidia/", "licenses/"))
                   or name == "gpu-runtime.json" for name in names)
        manifest = json.loads(archive.read("gpu-runtime.json"))
        listed = {row["path"]: row for row in manifest["files"]}
        assert set(names) - {"gpu-runtime.json"} == set(listed)
        assert sum(path.endswith(".dll") for path in listed) == 16
        for path, row in listed.items():
            payload = archive.read(path)
            assert len(payload) == row["size"]
            assert hashlib.sha256(payload).hexdigest() == row["sha256"]
        assert manifest["ctranslate2_version"] == "4.8.2"
        assert manifest["python_abi"] == "cp311"
        assert manifest["provenance"]["source_commit"] == "a" * 40


def test_gpu_pack_rejects_missing_license_and_invalid_commit(tmp_path):
    site = _gpu_site_packages(tmp_path)
    license_file = next(site.glob("nvidia_cublas_cu12-*.dist-info/licenses/LICENSE.txt"))
    license_file.unlink()
    with pytest.raises(ValueError, match="no license metadata"):
        gpu_pack.build(site, tmp_path / "bad.zip", version="1.0.0", python_abi="cp311",
                       source_commit="a" * 40)

    with pytest.raises(ValueError, match="Git SHA"):
        gpu_pack.build_manifest({}, version="1.0.0", python_abi="cp311",
                                ctranslate2_version="4.8.2", source_commit="bad",
                                dll_directories=[], distributions=[], license_metadata=[])




def test_gpu_pack_rejects_wrong_cuda_major_without_replacing_previous_archive(tmp_path):
    site = _gpu_site_packages(tmp_path)
    cublas_metadata = next(site.glob("nvidia_cublas_cu12-*.dist-info/METADATA"))
    cublas_metadata.write_text(
        "Metadata-Version: 2.1\nName: nvidia-cublas-cu12\nVersion: 11.8.0\n\n",
        encoding="utf-8")
    output = tmp_path / "existing.zip"
    output.write_bytes(b"previous-valid-archive")

    with pytest.raises(ValueError, match="not CUDA/cuDNN major 12"):
        gpu_pack.build(site, output, version="1.0.0", python_abi="cp311",
                       source_commit="a" * 40)
    assert output.read_bytes() == b"previous-valid-archive"
    assert not list(tmp_path.glob("existing.zip.*.incomplete"))
