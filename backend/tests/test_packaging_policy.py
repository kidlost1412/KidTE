import importlib.util
import json
from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "stage_runtime", PROJECT / "packaging" / "stage_runtime.py"
)
stage_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage_runtime)


def test_all_manifest_scripts_are_present_in_staging_allowlist():
    for folder, allowed in (('extension', stage_runtime.EXTENSION_FILES),
                            ('extension-shopee', stage_runtime.SHOPEE_EXTENSION_FILES)):
        manifest = json.loads((PROJECT / folder / 'manifest.json').read_text(encoding='utf-8'))
        scripts = {script for row in manifest.get('content_scripts', []) for script in row.get('js', [])}
        worker = manifest.get('background', {}).get('service_worker')
        if worker: scripts.add(worker)
        assert scripts <= set(allowed), scripts - set(allowed)
        assert 'message-delivery.js' in scripts


def _ui_assets(goc: Path):
    """Minimal PyInstaller web payload required before staging external files."""
    for relative in (
        "_internal/backend/src/web/operator.html",
        "_internal/backend/src/web/kid-te-icon.png",
        "_internal/backend/src/web/app/bootstrap.js",
        "_internal/backend/src/web/styles/tokens.css",
        "_internal/backend/prompts/system_prompt.txt",
        "_internal/backend/prompts/sales_playbook.default.json",
        "_internal/backend/prompts/knowledge_seed.json",
        "_internal/certifi/cacert.pem",
        "_internal/nvidia/cublas/bin/cublas64_12.dll",
        "_internal/nvidia/cudnn/bin/cudnn64_9.dll",
    ):
        path = goc / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("asset", encoding="utf-8")
    sidecar = goc / "Captcha" / "KidTECaptcha.exe"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_bytes(b"separate-executable")
    slider = goc / "Captcha" / "_internal" / "tiktok_live_studio_captcha_solver" / "resources" / "modern_puzzle_slider.png"
    slider.parent.mkdir(parents=True, exist_ok=True)
    slider.write_bytes(b"png")
    for name in ("monitor.ps1", "record-and-send.ps1"):
        script = goc / "Captcha" / "_internal" / "captcha-monitor" / name
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# read only monitor", encoding="utf-8")


def test_stage_runtime_copies_allowlist_only(tmp_path, monkeypatch):
    project = tmp_path / "project"
    destination = tmp_path / "dist"
    for executable in ("KidTE.exe", "KidTEVideo.exe"):
        destination.mkdir(exist_ok=True)
        (destination / executable).write_bytes(b"exe")
    _ui_assets(destination)
    for plugin_id, names in stage_runtime.PLUGIN_FILES.items():
        for name in names:
            path = project / "plugins" / plugin_id / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}" if name.endswith(".json") else "# code", encoding="utf-8")
    for name in stage_runtime.EXTENSION_FILES:
        path = project / "extension" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if name.endswith(".json") else "asset", encoding="utf-8")
    for name in stage_runtime.SHOPEE_EXTENSION_FILES:
        path = project / "extension-shopee" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}" if name.endswith(".json") else "asset", encoding="utf-8")
    for name in stage_runtime.DOC_FILES:
        path = project / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("guide", encoding="utf-8")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        path = project / "tools" / "ffmpeg" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"exe")
    (project / "backend" / ".env").parent.mkdir(parents=True)
    (project / "backend" / ".env").write_text("SECRET=1", encoding="utf-8")
    monkeypatch.setattr(stage_runtime, "PROJECT", project)
    # The staging gate runs against the same checked-in theme source, not a
    # permissive mock checker in this privacy/allowlist fixture.
    import shutil
    for relative in ("packaging/apply_theme.py", "packaging/sync_extension_ui.py", "ui/extension/popup.html", "ui/theme/sakura.json",
                     "backend/src/web/styles/tokens.css", "ui/extension/popup.css",
                     "ui/extension/live-widget.js"):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / relative, target)
    for name in ("popup.css", "popup-ux.js", "live-widget.js"):
        target = project / "ui" / "extension" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("asset", encoding="utf-8")
        for folder in ("extension", "extension-shopee"):
            (project / folder / name).write_bytes(target.read_bytes())
    for folder in ('extension','extension-shopee'):
        shutil.copyfile(PROJECT / folder / 'popup.html', project / folder / 'popup.html')
    monkeypatch.setattr(stage_runtime.shutil, "which", lambda _name: None)
    result = stage_runtime.stage(destination)
    assert result["ok"] is True
    assert not (destination / "backend" / ".env").exists()
    assert not any("test-" in item.name for item in (destination / "extension").iterdir())
    assert (destination / "extension-shopee" / "manifest.json").is_file()
    assert not any("test-" in item.name for item in (destination / "extension-shopee").iterdir())
    # Số phiên bản do lúc build tự sinh (VERSION + số commit), không ghi cứng.
    assert (destination / "VERSION").read_text(encoding="utf-8").strip().startswith("0.0.")


def test_build_version_uses_version_file_and_commit_count(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("3.4\n", encoding="utf-8")
    monkeypatch.setattr(stage_runtime, "PROJECT", tmp_path)

    class Done:
        stdout = "57\n"
    monkeypatch.setattr(stage_runtime.subprocess, "run", lambda *a, **k: Done())
    assert stage_runtime.build_version() == "3.4.57"


def test_validator_rejects_secrets_and_runtime_data(tmp_path):
    for item in ("KidTE.exe", "KidTEVideo.exe"):
        (tmp_path / item).write_bytes(b"exe")
    (tmp_path / "extension").mkdir()
    (tmp_path / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    for plugin_id in stage_runtime.PLUGIN_FILES:
        path = tmp_path / "plugins" / plugin_id / "plugin.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(RuntimeError, match="forbidden"):
        stage_runtime.validate(tmp_path)


def _bo_khung_hop_le(goc: Path):
    """Dung mot thu muc dong goi toi thieu, hop le, de them tung tep xau vao."""
    for item in ("KidTE.exe", "KidTEVideo.exe", "VERSION"):
        (goc / item).write_bytes(b"exe")
    _ui_assets(goc)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        path = goc / "tools" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"exe")
    (goc / "extension").mkdir(exist_ok=True)
    (goc / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    (goc / "extension-shopee").mkdir(exist_ok=True)
    (goc / "extension-shopee" / "manifest.json").write_text("{}", encoding="utf-8")
    for folder, names in [('extension',stage_runtime.EXTENSION_FILES), ('extension-shopee',stage_runtime.SHOPEE_EXTENSION_FILES), ('docs',stage_runtime.DOC_FILES)]:
        for name in names:
            asset = goc / folder / name
            asset.parent.mkdir(parents=True, exist_ok=True)
            asset.write_bytes(b"asset")
    for plugin_id, names in stage_runtime.PLUGIN_FILES.items():
        for name in names:
            path = goc / "plugins" / plugin_id / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}" if name.endswith(".json") else "# code", encoding="utf-8")
    return goc


def test_khung_toi_thieu_la_hop_le(tmp_path):
    """Neu khung nay tu no da bi tu choi thi cac test duoi vo nghia."""
    assert stage_runtime.validate(_bo_khung_hop_le(tmp_path))["ok"] is True


@pytest.mark.parametrize("duong_dan", [
    # `[F09]` Du lieu rieng cua may van hanh, truoc day deu lot qua vi bo kiem
    # cu chi chan 4 ten tep va hai duoi .pyc/.log.
    "backend/data/videos/live2/transcript.json",
    "backend/data/videos/live2/fingerprint.npz",
    "backend/data/videos/live2/timeline.json",
    "backend/data/videos/live2/timeline.review.json",
    "backend/data/videos/live2/ANALYSIS.md",
    "backend/data/machine/catalog_mapping.json",
    "DuLieu/data/operator_workspace.sqlite3",
    "DuLieu/data/video_uploads/abc/live-2.mp4",
    "DuLieu/logs/raw_stream/2026-09-15.jsonl",
    "DuLieu/data/trash/mot-thu-gi-do.json",
    "DuLieu/random_private_file.json",
    "backend/data/unlisted_private_file.json",
    "_internal/backend/data/unlisted_private_file.json",
    # Máy mới cài phải TRẮNG: không mang hồ sơ sản phẩm / thông tin shop / Trí nhớ AI.
    "_internal/backend/data/shared/product_knowledge.json",
    "_internal/backend/data/thong_tin_shop.json",
    "_internal/backend/data/approved_live_profiles.json",
    "DuLieu/data/ai_learning.sqlite3",
])
def test_bo_kiem_dong_goi_tu_choi_du_lieu_rieng(tmp_path, duong_dan):
    goc = _bo_khung_hop_le(tmp_path)
    xau = goc / duong_dan
    xau.parent.mkdir(parents=True, exist_ok=True)
    xau.write_text("du lieu rieng", encoding="utf-8")
    with pytest.raises(RuntimeError, match="forbidden") as ctx:
        stage_runtime.validate(goc)
    assert duong_dan in str(ctx.value)


def test_seed_phat_hanh_khop_giua_spec_va_runtime_layout():
    """`[F09]` Hai danh sach seed phai giong nhau va khong duoc co du lieu rieng.

    Truoc day ca hai deu liet ke `data/videos`, nen moi may cai dat deu nhan
    transcript, fingerprint va timeline cua dung mot may khac.
    """
    import ast
    import re
    import sys
    sys.path.insert(0, str(PROJECT / "backend" / "src"))
    spec_text = (PROJECT / "packaging" / "KidTE.spec").read_text(encoding="utf-8")
    # The spec has no seed-copy loop. Reject data paths in any literal,
    # including a future accidental addition to core_datas.
    private_paths = [node.value for node in ast.walk(ast.parse(spec_text))
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and (node.value == "data" or node.value.startswith("data/"))]
    assert not private_paths, f"Spec bundles private data: {private_paths}"
    trong_spec = ()

    layout_text = (PROJECT / "backend" / "src" / "runtime_layout.py").read_text(
        encoding="utf-8")
    khoi2 = re.search(r"SEED_PATHS = \((.*?)\)", layout_text, re.S)
    assert khoi2, "khong doc duoc SEED_PATHS"
    trong_layout = tuple(re.findall(r'"([^"]+)"', khoi2.group(1)))

    assert trong_spec == trong_layout, (
        f"KidTE.spec seed {trong_spec} khac runtime_layout {trong_layout}")
    for duong_dan in trong_spec:
        assert "videos" not in duong_dan, (
            f"{duong_dan}: timeline/transcript/fingerprint la du lieu rieng, "
            f"khong duoc di theo ban phat hanh")
        assert "machine" not in duong_dan, f"{duong_dan}: du lieu rieng cua may"


@pytest.mark.parametrize("relative", ["extension/popup.html", "extension-shopee/ai-controller.js", "extension-shopee/icons/kid-te-128.png"])
def test_runtime_rejects_missing_extension_asset(tmp_path, relative):
    root = _bo_khung_hop_le(tmp_path)
    (root / relative).unlink()
    with pytest.raises(RuntimeError, match="missing") as exc:
        stage_runtime.validate(root)
    assert relative in str(exc.value)


def test_ffmpeg_from_project_tools_is_bundled(tmp_path, monkeypatch):
    project = tmp_path / "project"
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        path = project / "tools" / "ffmpeg" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"exe")
    monkeypatch.setattr(stage_runtime, "PROJECT", project)
    dest = tmp_path / "dist"
    assert stage_runtime.copy_ffmpeg(dest) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (dest / "tools" / "ffprobe.exe").read_bytes() == b"exe"


def test_stage_rejects_missing_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(stage_runtime, "PROJECT", tmp_path)
    monkeypatch.setattr(stage_runtime.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Thiếu ffmpeg.exe hoặc ffprobe.exe"):
        stage_runtime.copy_ffmpeg(tmp_path / "dist")
