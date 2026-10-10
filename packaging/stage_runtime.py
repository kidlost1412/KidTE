"""Copy only audited external runtime assets into the PyInstaller folder."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
PLUGIN_FILES = {
    "lieu-dung": ("plugin.json", "processor.py"),
    "chinh-sach-gia": ("plugin.json", "processor.py"),
    "thu-du-lieu": ("plugin.json", "processor.py"),
    "model-deepseek": ("plugin.json", "processor.py"),
    "chon-san-pham": ("plugin.json", "processor.py"),
    "dong-bo-video": ("plugin.json", "chay.py", "live_sync.py", "video_sync.py"),
}
EXTENSION_FILES = (
    "manifest.json", "background.js", "comments.js", "content.js", "message-delivery.js",
    "popup.html", "popup.css", "popup-ux.js", "live-widget.js", "popup.js", "schedule.js", "README.md",
    "icons/kid-te-16.png", "icons/kid-te-32.png",
    "icons/kid-te-48.png", "icons/kid-te-128.png",
)
SHOPEE_EXTENSION_FILES = (
    "manifest.json", "background.js", "controller.js", "ai-controller.js", "core.js", "message-delivery.js",
    "shopee-api.js", "popup.html", "popup.css", "popup-ux.js", "live-widget.js", "popup.js", "README.md",
    "icons/kid-te-16.png", "icons/kid-te-32.png", "icons/kid-te-48.png", "icons/kid-te-128.png",
)
DOC_FILES = ("CAI-DAT-TIEN-ICH-CHROME.md", "CAI-DAT-WINDOWS.md", "TICH-HOP-SHOPEE-LIVE.md",
             "TICH-HOP-CAPTCHA.md")
# Tên tệp không bao giờ được xuất hiện trong bản đóng gói.
#
# `[F09]` Bản cũ chỉ chặn 4 tên dưới đây, nên toàn bộ `data/videos` của máy
# vận hành — transcript (lời nói thật của chủ shop trong phiên LIVE),
# fingerprint và timeline ghim sản phẩm — vẫn đi thẳng vào EXE và bộ cài.
# Kiểm bằng phần mở rộng media là chưa đủ: ba tệp đó đều là .json/.npz.
FORBIDDEN_NAMES = {".env", "customer_memory.sqlite3", "shopee_customers.sqlite3",
                   "operator_workspace.sqlite3",
                   "live_products_current.json", "live_products_raw_current.json",
                   "transcript.json", "fingerprint.npz",
                   "timeline.json", "timeline.review.json",
                   "draft.json", "job.json",
                   # Dữ liệu của shop đang phát triển: máy mới cài phải trắng.
                   "product_knowledge.json", "approved_live_profiles.json",
                   "thong_tin_shop.json", "company_catalog_mapping.json",
                   "local_product_overrides.json", "catalog_mapping.json",
                   "shopee_live_mappings.json", "ai_learning.sqlite3"}
# Dữ liệu vận hành nhận theo phần mở rộng, bất kể tên là gì.
FORBIDDEN_SUFFIXES = {".pyc", ".log", ".sqlite3", ".jsonl", ".part",
                      ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wav"}
# Thư mục chỉ chứa dữ liệu riêng hoặc phụ trợ phát triển.
FORBIDDEN_PARTS = {"__pycache__", "raw_stream", "backups", "reports", "tests",
                   "videos", "video_uploads", "machine", "trash",
                   "customer_sessions", "missing_answers", "metrics"}

# Local model weights and caches belong in DuLieu and are never installer input.
# CTranslate2's `ctranslate2/models` package is library code, not model weights.
USER_MODEL_DIRS = {"dulieu", "models", "model_cache", "whisper_models",
                   "huggingface", "hub"}
MODEL_WEIGHT_SUFFIXES = {".bin", ".safetensors", ".pt", ".pth", ".ckpt", ".gguf"}
RUNTIME_ONNX_ALLOWLIST = {
    "_internal/faster_whisper/assets/silero_vad_v6.onnx",
    "faster_whisper/assets/silero_vad_v6.onnx",
}
GPU_DLL_ROOT = "_internal/nvidia/"


def copy_file(source: Path, target: Path):
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def build_version():
    """``VERSION`` (phần chính, vd 1.4) + số commit → 1.4.96. Không ghi cứng ở đâu.

    Không có git (máy build lạ) thì dùng ngày build, vẫn luôn tăng dần.
    """
    explicit = os.environ.get("KIDTE_RELEASE_VERSION", "").strip()
    if explicit:
        if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", explicit):
            raise ValueError("KIDTE_RELEASE_VERSION must be a three-part numeric version")
        return explicit
    try:
        base = (PROJECT / "VERSION").read_text(encoding="utf-8").strip() or "0.0"
    except OSError:
        base = "0.0"
    try:
        count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=PROJECT,
                               capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        if not count.isdigit():
            raise ValueError(count)
    except (OSError, subprocess.SubprocessError, ValueError):
        count = datetime.now(timezone.utc).strftime("%y%m%d")
    return f"{base}.{count}"


def stage(destination: Path, profile: str = "gpu"):
    subprocess.run([sys.executable, str(PROJECT / 'packaging/apply_theme.py'), '--check'], check=True)
    subprocess.run([sys.executable, str(PROJECT / 'packaging/sync_extension_ui.py'), '--check'], check=True)
    destination = destination.resolve()
    if not (destination / "KidTE.exe").is_file():
        raise RuntimeError("Thư mục đích chưa có KidTE.exe")
    (destination / "VERSION").write_text(build_version() + "\n", encoding="utf-8")
    for plugin_id, names in PLUGIN_FILES.items():
        for name in names:
            copy_file(PROJECT / "plugins" / plugin_id / name,
                      destination / "plugins" / plugin_id / name)
    for name in EXTENSION_FILES:
        copy_file(PROJECT / "extension" / name, destination / "extension" / name)
    for name in SHOPEE_EXTENSION_FILES:
        copy_file(PROJECT / "extension-shopee" / name,
                  destination / "extension-shopee" / name)
    for name in DOC_FILES:
        copy_file(PROJECT / "docs" / name, destination / "docs" / name)
    copy_ffmpeg(destination)
    return validate(destination, profile=profile)


def copy_ffmpeg(destination: Path):
    """ffmpeg/ffprobe đi kèm bộ cài (Studio + phân tích video cần). Lấy từ
    ``tools/ffmpeg/`` của dự án nếu có, không thì từ PATH của máy build."""
    copied = []
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        source = PROJECT / "tools" / "ffmpeg" / name
        if not source.is_file():
            found = shutil.which(name)
            source = Path(found) if found else None
        if source and source.is_file():
            copy_file(source, destination / "tools" / name)
            copied.append(name)
    if len(copied) < 2:
        raise RuntimeError("Thiếu ffmpeg.exe hoặc ffprobe.exe: không tạo bộ cài thiếu tính năng video")
    return copied


def _is_ctranslate2_library_models(relative: Path) -> bool:
    parts = tuple(part.lower() for part in relative.parts)
    return any(parts[index:index + 2] == ("ctranslate2", "models")
               for index in range(max(0, len(parts) - 1)))


def _is_runtime_onnx(relative: Path) -> bool:
    normalized = relative.as_posix().replace("\\", "/").lower()
    return normalized in RUNTIME_ONNX_ALLOWLIST


def _model_policy_violation(relative: Path) -> bool:
    """Reject user weights/caches while allowing runtime modules and bundled VAD."""
    parts = tuple(part.lower() for part in relative.parts)
    if _is_ctranslate2_library_models(relative):
        # This is the CTranslate2 package's model implementation module tree,
        # not an application model cache. Weight formats remain forbidden here.
        if relative.suffix.lower() in MODEL_WEIGHT_SUFFIXES or relative.suffix.lower() == ".onnx":
            return True
        return relative.suffix.lower() not in {".py", ".pyi", ".pyd", ".dll"}
    if USER_MODEL_DIRS.intersection(parts):
        return True
    if relative.suffix.lower() in MODEL_WEIGHT_SUFFIXES:
        return True
    return relative.suffix.lower() == ".onnx" and not _is_runtime_onnx(relative)


def _gpu_dlls(destination: Path):
    root = Path(destination)
    nvidia_prefixes = ("cublas", "cudnn", "nvrtc", "nvjitlink", "nvblas",
                       "cusparse", "curand", "cufft", "cusolver", "npp",
                       "nvjpeg", "nvcuda", "nvoptix")
    found = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".dll":
            continue
        relative = path.relative_to(root)
        windows_parts = tuple(part.lower() for part in relative.parts)
        name = path.name.lower()
        if ("nvidia" in windows_parts or relative.as_posix().lower().startswith(GPU_DLL_ROOT)
                or name.startswith(nvidia_prefixes)):
            found.append(relative.as_posix())
    return found


def validate(destination: Path, profile: str = "gpu"):
    """Validate CPU or legacy bundled-GPU payloads.

    Keep the direct API default at `gpu` for existing callers. Build scripts
    should pass an explicit profile; the command line defaults to CPU.
    """
    profile = str(profile).strip().lower()
    if profile not in {"cpu", "gpu"}:
        raise ValueError("profile must be 'cpu' or 'gpu'")
    destination = Path(destination).resolve()
    required = ["KidTE.exe", "KidTEVideo.exe", "Captcha/KidTECaptcha.exe",
                "Captcha/_internal/tiktok_live_studio_captcha_solver/resources/modern_puzzle_slider.png",
                "Captcha/_internal/captcha-monitor/monitor.ps1",
                "Captcha/_internal/captcha-monitor/record-and-send.ps1", "VERSION",
                "tools/ffmpeg.exe", "tools/ffprobe.exe",
                "extension/manifest.json",
                "extension-shopee/manifest.json",
                "extension/icons/kid-te-16.png",
                "extension/icons/kid-te-32.png",
                "extension/icons/kid-te-48.png",
                "extension/icons/kid-te-128.png",
                "_internal/backend/src/web/operator.html",
                "_internal/backend/src/web/kid-te-icon.png",
                "_internal/backend/src/web/app/bootstrap.js",
                "_internal/backend/src/web/styles/tokens.css",
                "_internal/backend/prompts/system_prompt.txt",
                "_internal/backend/prompts/sales_playbook.default.json",
                # Gói hồ sơ chính thức: máy trắng tự cài từ đây lần đầu mở.
                "_internal/backend/prompts/knowledge_seed.json",
                "_internal/certifi/cacert.pem"]
    if profile == "gpu":
        required += ["_internal/nvidia/cublas/bin/cublas64_12.dll",
                     "_internal/nvidia/cudnn/bin/cudnn64_9.dll"]
    required += [f"plugins/{plugin_id}/plugin.json" for plugin_id in PLUGIN_FILES]
    required += [f"plugins/{plugin_id}/{name}" for plugin_id, names in PLUGIN_FILES.items() for name in names]
    required += [f"extension/{name}" for name in EXTENSION_FILES]
    required += [f"extension-shopee/{name}" for name in SHOPEE_EXTENSION_FILES]
    required += [f"docs/{name}" for name in DOC_FILES]
    missing = [item for item in required if not (destination / item).is_file()]
    forbidden = []
    if profile == "cpu":
        forbidden.extend(_gpu_dlls(destination))
    for item in destination.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(destination)
        parts = relative.parts
        folded_parts = tuple(part.lower() for part in parts)
        private_tree = (folded_parts[0] == "dulieu"
                        or folded_parts[:2] == ("backend", "data")
                        or folded_parts[:3] == ("_internal", "backend", "data"))
        if (private_tree or item.name.lower() in {name.lower() for name in FORBIDDEN_NAMES}
                or item.suffix.lower() in FORBIDDEN_SUFFIXES
                or FORBIDDEN_PARTS.intersection(folded_parts)
                or _model_policy_violation(relative)):
            forbidden.append(relative.as_posix())
    if missing or forbidden:
        raise RuntimeError(json.dumps({"missing": missing, "forbidden": forbidden}, ensure_ascii=False))
    return {"ok": True, "profile": profile,
            "files": sum(1 for item in destination.rglob("*") if item.is_file())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--profile", type=str.lower, choices=("cpu", "gpu"), default="cpu")
    args = parser.parse_args()
    result = (validate(args.destination.resolve(), profile=args.profile) if args.validate_only
              else stage(args.destination, profile=args.profile))
    print(json.dumps(result, ensure_ascii=False))
