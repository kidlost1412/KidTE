"""Stage optional runtime packages separately from the minimal installer tree."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import stage_runtime


def stage_components(dist: Path, destination: Path):
    dist, destination = dist.resolve(), destination.absolute()
    if destination.exists():
        raise FileExistsError("component output must be a new directory")
    destination.mkdir(parents=True)
    worker = destination / "video-analysis-cpu"
    shutil.copytree(dist / "KidTETranscription", worker)
    video = destination / "video-sync"
    shutil.copytree(dist / "KidTEVideo", video)
    for name in stage_runtime.PLUGIN_FILES["dong-bo-video"]:
        stage_runtime.copy_file(stage_runtime.PROJECT / "plugins/dong-bo-video" / name,
                                video / "plugins/dong-bo-video" / name)
    captcha = destination / "captcha-solver" / "Captcha"
    shutil.copytree(dist / "KidTECaptcha", captcha)
    media = destination / "media-tools"
    stage_runtime.copy_ffmpeg(media)
    source = Path(shutil.which("ffmpeg.exe") or "").resolve()
    project_source = stage_runtime.PROJECT / "tools/ffmpeg/ffmpeg.exe"
    if project_source.is_file():
        source = project_source.resolve()
    vendor_root = source.parent.parent
    license_path = vendor_root / "LICENSE"
    if not license_path.is_file():
        raise FileNotFoundError("FFmpeg vendor LICENSE is required beside the vendor bin folder")
    stage_runtime.copy_file(license_path, media / "licenses/FFmpeg-LICENSE.txt")
    readme = vendor_root / "README.txt"
    if readme.is_file():
        stage_runtime.copy_file(readme, media / "licenses/FFmpeg-README.txt")
    version = subprocess.run([str(source), "-version"], capture_output=True, text=True,
                             timeout=30, check=True).stdout
    (media / "provenance.json").write_text(json.dumps({
        "vendor": "Gyan FFmpeg", "build_information": version,
        "builds_url": "https://www.gyan.dev/ffmpeg/builds/",
        "source_url": "https://github.com/FFmpeg/FFmpeg/tree/n9.0",
        "license": "GPL-3.0-or-later",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: str(destination / key) for key in (
        "media-tools", "captcha-solver", "video-sync", "video-analysis-cpu")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(stage_components(args.dist, args.destination)))
