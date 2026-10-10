"""Copy the reviewed, source-only KidTE tree to a fresh public-repo directory.

This tool never edits or deletes its source. It intentionally does not copy Git
history; the destination must not exist so it cannot erase a previous checkout.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT_FILES = {".gitattributes", ".gitignore", "README.md", "VERSION"}
SOURCE_ROOTS = {"assets", "backend", "extension", "extension-shopee", "packaging", "plugins", "ui"}
PUBLIC_DOCS = {
    "docs/CAI-DAT-TIEN-ICH-CHROME.md",
    "docs/CAI-DAT-WINDOWS.md",
    "docs/CAP-NHAT-GITHUB.md",
    "docs/GOI-GPU-TUY-CHON.md",
    "docs/PROVENANCE-CAPTCHA-GUARD.md",
    "docs/THEM-VIDEO.md",
    "docs/TICH-HOP-CAPTCHA.md",
    "docs/TICH-HOP-SHOPEE-LIVE.md",
    "docs/schema-baseline.json",
}
BLOCKED_ANYWHERE = {
    ".git", ".pytest_cache", ".ruff_cache", ".venv", ".venv-cpu-build",
    "__pycache__", "node_modules",
}
BLOCKED_NAMES = {".env", "journal.jsonl", "live_sources.json", "live_state.json", "privacy_key"}
BLOCKED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pyc"}


class PublicSourceError(ValueError):
    """The input or destination is unsafe for a public-source copy."""


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attrs & reparse_flag)


def _included(rel: PurePosixPath) -> tuple[bool, str]:
    value = rel.as_posix()
    parts = rel.parts
    if len(parts) == 1 and value in ROOT_FILES:
        return True, "root source file"
    if parts[0] == "docs":
        return (True, "approved operator documentation") if value in PUBLIC_DOCS else (False, "docs not on public documentation allowlist")
    if parts[0] not in SOURCE_ROOTS:
        return False, "outside source allowlist"
    lowered = tuple(part.lower() for part in parts)
    if any(part in BLOCKED_ANYWHERE for part in lowered):
        return False, "runtime data, build output, or local environment"
    if lowered[0] in {"build", "dist", "test-artifacts", "tiktok-captcha-solver-logs"}:
        return False, "runtime data, build output, or local environment"
    if len(lowered) >= 2 and lowered[:2] in {
        ("backend", "data"), ("backend", "logs"), ("backend", "backups"),
        ("backend", "runtime"), ("backend", "reports"), ("backend", "exports"),
        ("backend", "models"), ("backend", "captcha"),
    }:
        return False, "runtime data, build output, or local environment"
    if any(part.lower() == ".env" or (part.lower().startswith(".env.") and part != ".env.example") for part in parts):
        return False, "environment secret/configuration file"
    blocked_suffix = Path(parts[-1]).suffix.lower() in BLOCKED_SUFFIXES
    synthetic_test_fixture = value.startswith("backend/tests/fixtures/")
    if parts[-1] in BLOCKED_NAMES or (blocked_suffix and not synthetic_test_fixture):
        return False, "runtime state or log file"
    return True, "source code or reviewed synthetic fixture"


def _git_files(source: Path) -> list[PurePosixPath]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-co", "--exclude-standard", "-z"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicSourceError(f"cannot enumerate Git source files: {exc}") from exc
    return [PurePosixPath(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def prepare_public_source(source: Path, destination: Path) -> dict[str, object]:
    source = source.expanduser().absolute()
    if any(_is_reparse(parent) for parent in (source, *source.parents)):
        raise PublicSourceError("source path contains a symlink or junction")
    if not source.is_dir():
        raise PublicSourceError("source must be a directory")
    try:
        git_root = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PublicSourceError(f"source must be the root of a Git working tree: {exc}") from exc
    if os.path.normcase(os.path.abspath(git_root)) != os.path.normcase(str(source)):
        raise PublicSourceError("source must be the Git working-tree root")
    destination = destination.expanduser().absolute()
    if not destination.parent.is_dir():
        raise PublicSourceError("destination parent must already exist")
    if any(_is_reparse(parent) for parent in (destination.parent, *destination.parent.parents)):
        raise PublicSourceError("destination path contains a symlink or junction")
    if destination.exists() or destination.is_symlink():
        raise PublicSourceError("destination must be a new, nonexistent directory")
    if destination == source or source in destination.parents or destination in source.parents:
        raise PublicSourceError("source and destination must be separate, non-nested directories")

    included: list[tuple[PurePosixPath, Path]] = []
    skipped: list[dict[str, str]] = []
    for rel in _git_files(source):
        allow, reason = _included(rel)
        src = source.joinpath(*rel.parts)
        if not allow:
            skipped.append({"path": rel.as_posix(), "reason": reason})
            continue
        if not src.exists():
            skipped.append({"path": rel.as_posix(), "reason": "tracked file missing from working tree"})
            continue
        # Refuse links even when they currently resolve inside the checkout.
        cursor = source
        unsafe = False
        for part in rel.parts:
            cursor = cursor / part
            if _is_reparse(cursor):
                unsafe = True
                break
        if unsafe or not src.is_file():
            skipped.append({"path": rel.as_posix(), "reason": "symlink, junction, or non-regular file"})
            continue
        included.append((rel, src))

    if not included:
        raise PublicSourceError("public source allowlist matched no files")

    destination.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    try:
        for rel, src in included:
            target = destination.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if any(_is_reparse(parent) for parent in (target.parent, *target.parents)):
                raise PublicSourceError(f"destination path became a reparse point: {rel.as_posix()}")
            shutil.copyfile(src, target)
            shutil.copystat(src, target, follow_symlinks=False)
            copied.append(rel.as_posix())
    except Exception:
        # Only remove the fresh destination created by this call.
        shutil.rmtree(destination)
        raise

    return {
        "source": str(source),
        "destination": str(destination),
        "copied_count": len(copied),
        "copied_bytes": sum((source / Path(*PurePosixPath(p).parts)).stat().st_size for p in copied),
        "copied": copied,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="existing Git working tree")
    parser.add_argument("--destination", type=Path, required=True, help="new, nonexistent output directory")
    args = parser.parse_args(argv)
    try:
        report = prepare_public_source(args.source, args.destination)
    except PublicSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Copied {report['copied_count']} files ({report['copied_bytes']} bytes) to {report['destination']}.")
    print(f"Skipped {report['skipped_count']} files outside the public-source policy:")
    for item in report["skipped"]:
        print(f"  {item['path']} — {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
