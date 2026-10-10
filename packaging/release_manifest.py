"""Sinh va kiem tra manifest ban phat hanh (G06).

generate <dist_dir> [--out FILE] [--project DIR] [--python PY]
verify   <dist_dir> [--manifest FILE]   (thoat khac 0 neu bi sua/thieu/thua file)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = "release-manifest.json"
EXE_SUFFIXES = {".exe", ".dll", ".pyd"}
SCHEMA = 1


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def git_rev(root: Path | None) -> str:
    if not root or not Path(root).is_dir():
        return ""
    return _git(Path(root), "rev-parse", "HEAD")


def _run_py(python: str, code: str) -> str:
    try:
        r = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


_DEPS_CODE = r"""
import json, hashlib
from importlib import metadata
out = []
for d in metadata.distributions():
    name = d.metadata["Name"]
    if not name:
        continue
    digest = ""
    for f in d.files or []:
        if f.name == "RECORD":
            try:
                digest = hashlib.sha256(open(d.locate_file(f), "rb").read()).hexdigest()
            except OSError:
                pass
            break
    out.append({"name": name, "version": d.version, "sha256": digest})
out.sort(key=lambda x: x["name"].lower())
print(json.dumps(out))
"""


def freeze_deps(python: str | None = None) -> list[dict]:
    """Dist da cai trong venv build: ten, phien ban, sha256 cua RECORD (khoa noi dung)."""
    raw = _run_py(python or sys.executable, _DEPS_CODE)
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


def _walk(dist: Path):
    for p in sorted(dist.rglob("*")):
        if p.is_file() and p.name != MANIFEST_NAME:
            yield p


def hash_dist(dist: Path) -> tuple[list[dict], list[dict]]:
    exe, other = [], []
    for p in _walk(dist):
        rec = {"file": p.relative_to(dist).as_posix(), "sha256": sha256_file(p)}
        (exe if p.suffix.lower() in EXE_SUFFIXES else other).append(rec)
    return exe, other


def hash_solver_patches(project: Path) -> list[dict]:
    d = Path(project) / "packaging" / "solver-patches"
    if not d.is_dir():
        return []
    return [{"file": p.relative_to(d).as_posix(), "sha256": sha256_file(p)}
            for p in sorted(d.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]


def _py_version(python: str | None) -> str:
    if python in (None, sys.executable):
        return sys.version.split()[0]
    try:
        r = subprocess.run([python, "--version"], capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr).strip().replace("Python ", "")
    except (OSError, subprocess.SubprocessError):
        return ""


def build_manifest(dist: Path, project: Path, solver_root: Path | None = None,
                   python: str | None = None, deps: list[dict] | None = None) -> dict:
    dist = Path(dist)
    vf = dist / "VERSION"
    exe, other = hash_dist(dist)
    return {
        "schema_version": SCHEMA,
        "version": vf.read_text(encoding="utf-8").strip() if vf.is_file() else "",
        "git_sha": _git(Path(project), "rev-parse", "HEAD"),
        "python": _py_version(python),
        "solver_commit": git_rev(solver_root),
        "deps": freeze_deps(python) if deps is None else deps,
        "solver_patches": hash_solver_patches(project),
        "exe": exe,
        "files": other,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_manifest(manifest: dict, out: Path) -> Path:
    out = Path(out)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def verify(dist: Path, manifest_path: Path | None = None) -> list[str]:
    """Tra ve danh sach loi; rong = khop."""
    dist = Path(dist)
    mp = Path(manifest_path) if manifest_path else dist / MANIFEST_NAME
    if not mp.is_file():
        return [f"thieu manifest: {mp}"]
    m = json.loads(mp.read_text(encoding="utf-8"))
    expected = {r["file"]: r["sha256"] for r in m.get("exe", []) + m.get("files", [])}
    errors = []
    for rel, digest in expected.items():
        p = dist / rel
        if not p.is_file():
            errors.append(f"thieu file: {rel}")
        elif sha256_file(p) != digest:
            errors.append(f"sai hash: {rel}")
    for p in _walk(dist):
        rel = p.relative_to(dist).as_posix()
        if rel not in expected:
            errors.append(f"file la: {rel}")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("dist")
    g.add_argument("--out")
    g.add_argument("--project", default=str(Path(__file__).resolve().parents[1]))
    g.add_argument("--python")
    v = sub.add_parser("verify")
    v.add_argument("dist")
    v.add_argument("--manifest")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        dist = Path(a.dist)
        solver = os.environ.get("KIDTE_CAPTCHA_SOLVER_ROOT", "").strip()
        m = build_manifest(dist, Path(a.project), Path(solver) if solver else None, a.python)
        out = write_manifest(m, Path(a.out) if a.out else dist / MANIFEST_NAME)
        print(f"Manifest: {out}")
        return 0
    errs = verify(Path(a.dist), Path(a.manifest) if a.manifest else None)
    for e in errs:
        print("LOI:", e)
    print("OK" if not errs else f"THAT BAI ({len(errs)} loi)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
