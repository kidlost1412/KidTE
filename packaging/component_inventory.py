"""Report measured file ownership, SHA256s and byte totals for a staged Kid TE tree.

The inventory counts bytes on disk. Compressed sizes are reported only for an
artifact explicitly passed with ``--compressed-artifact``; no compression ratio
is estimated from the staged files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 1
OWNERSHIP_RULES = [
    {"component": "gpu-cuda12", "rule": "_internal/nvidia/** or nvidia/**"},
    {"component": "video-sync", "rule": "KidTEVideo.exe or plugins/dong-bo-video/**"},
    {"component": "captcha-monitor", "rule": "Captcha/_internal/captcha-monitor/**"},
    {"component": "captcha-solver", "rule": "remaining Captcha/**"},
    {"component": "media-tools", "rule": "tools/ffmpeg.exe or tools/ffprobe.exe"},
    {"component": "extension-tiktok", "rule": "extension/**"},
    {"component": "extension-shopee", "rule": "extension-shopee/**"},
    {"component": "documentation", "rule": "docs/**"},
    {"component": "core", "rule": "all remaining staged runtime files"},
]


def _owner(relative: str) -> str:
    path = relative.lower().replace("\\", "/")
    if path.startswith(("_internal/nvidia/", "nvidia/")):
        return "gpu-cuda12"
    if path == "kidtevideo.exe" or path.startswith("plugins/dong-bo-video/"):
        return "video-sync"
    if path.startswith("captcha/_internal/captcha-monitor/"):
        return "captcha-monitor"
    if path.startswith("captcha/"):
        return "captcha-solver"
    if path in {"tools/ffmpeg.exe", "tools/ffprobe.exe"}:
        return "media-tools"
    if path.startswith("extension-shopee/"):
        return "extension-shopee"
    if path.startswith("extension/"):
        return "extension-tiktok"
    if path.startswith("docs/"):
        return "documentation"
    return "core"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root: Path, compressed_artifact: Path | None = None) -> dict:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    entries = []
    totals: dict[str, int] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        if path.is_symlink():
            raise ValueError(f"symlink is not inventory-safe: {path.relative_to(root).as_posix()}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        owner = _owner(relative)
        totals[owner] = totals.get(owner, 0) + size
        entries.append({"path": relative, "component": owner, "size": size,
                        "sha256": _sha256(path)})

    report = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "ownership_rules": OWNERSHIP_RULES,
        "components": [
            {"component": owner, "raw_bytes": size}
            for owner, size in sorted(totals.items())
        ],
        "total_raw_bytes": sum(totals.values()),
        "files": entries,
    }
    if compressed_artifact is not None:
        artifact = Path(compressed_artifact).resolve(strict=True)
        if not artifact.is_file():
            raise FileNotFoundError(artifact)
        report["compressed_artifact"] = {
            "filename": artifact.name,
            "bytes": artifact.stat().st_size,
            "sha256": _sha256(artifact),
        }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="staged application directory")
    parser.add_argument("--compressed-artifact", type=Path,
                        help="measure this existing installer/archive's exact size and SHA256")
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    report = inventory(args.root, args.compressed_artifact)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.resolve()
        try:
            output.relative_to(Path(args.root).resolve())
        except ValueError:
            pass
        else:
            parser.error("--output must be outside the inventoried root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
