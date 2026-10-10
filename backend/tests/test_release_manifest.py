import importlib.util
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "release_manifest", PROJECT / "packaging" / "release_manifest.py")
rm = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rm)


def _dist(tmp_path):
    d = tmp_path / "KidTE"
    (d / "sub").mkdir(parents=True)
    (d / "KidTE.exe").write_bytes(b"exe")
    (d / "sub" / "a.txt").write_text("a")
    (d / "VERSION").write_text("1.2.3\n")
    return d


def _manifest(d):
    deps = [{"name": "x", "version": "1", "sha256": "ab"}]
    m = rm.build_manifest(d, PROJECT, solver_root=None, deps=deps)
    rm.write_manifest(m, d / rm.MANIFEST_NAME)
    return m


def test_manifest_lists_solver_hashes(tmp_path):
    m = _manifest(_dist(tmp_path))
    files = {r["file"]: r["sha256"] for r in m["solver_patches"]}
    assert "manifest.json" in files and len(files["manifest.json"]) == 64
    assert m["version"] == "1.2.3"
    assert [r["file"] for r in m["exe"]] == ["KidTE.exe"]
    assert {"solver_commit", "git_sha", "python", "deps", "built_at"} <= set(m)


def test_solver_commit_recorded_from_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "_git", lambda root, *a: "deadbeef")
    (tmp_path / "solver").mkdir()
    assert rm.git_rev(tmp_path / "solver") == "deadbeef"
    assert rm.git_rev(None) == ""


def test_verify_ok_then_detects_tamper(tmp_path):
    d = _dist(tmp_path)
    _manifest(d)
    assert rm.verify(d) == []
    (d / "KidTE.exe").write_bytes(b"evil")
    assert any("sai hash: KidTE.exe" in e for e in rm.verify(d))
    assert rm.main(["verify", str(d)]) == 1


def test_verify_detects_missing_and_extra(tmp_path):
    d = _dist(tmp_path)
    _manifest(d)
    (d / "sub" / "a.txt").unlink()
    (d / "new.dll").write_bytes(b"x")
    errs = rm.verify(d)
    assert any("thieu file" in e for e in errs) and any("file la" in e for e in errs)


def test_verify_requires_manifest(tmp_path):
    assert rm.verify(_dist(tmp_path))
