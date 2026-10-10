import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "prepare_public_source.py"
SPEC = importlib.util.spec_from_file_location("prepare_public_source", SCRIPT)
prepare = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(prepare)


def _git(source: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(source), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "config", "user.name", "Fixture")
    files = {
        "README.md": "public readme\n",
        "backend/src/app.py": "source\n",
        "backend/prompts/knowledge_seed.json": "approved baseline\n",
        "backend/data/private.json": "private data\n",
        "backend/tests/fixtures/live_titles.json": "synthetic titles\n",
        "backend/tests/fixtures/captcha/example.json": "synthetic captcha fixture\n",
        "backend/tests/fixtures/captcha/synthetic.log": "fixture log\n",
        "docs/schema-baseline.json": "baseline\n",
        "docs/kien-thuc/kiem-toan/private.md": "historical audit\n",
        "packaging/stage_runtime.py": "builder\n",
        "packaging/solver-patches/solver-after-legacy-guard.py.snapshot": "audited source patch\n",
        "dist/app.exe": "binary\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    # A clean, non-ignored P1/P2 source addition is included from the working tree.
    extra = source / "backend/src/update_manager.py"
    extra.write_text("update source\n", encoding="utf-8")
    return source


def test_public_copy_keeps_source_and_applies_path_policy(tmp_path):
    source = _source_tree(tmp_path)
    destination = tmp_path / "public"
    report = prepare.prepare_public_source(source, destination)

    assert (destination / "README.md").read_text(encoding="utf-8") == "public readme\n"
    assert (destination / "backend/prompts/knowledge_seed.json").is_file()
    assert (destination / "backend/tests/fixtures/live_titles.json").is_file()
    assert (destination / "backend/tests/fixtures/captcha/example.json").is_file()
    assert (destination / "backend/tests/fixtures/captcha/synthetic.log").is_file()
    assert (destination / "backend/src/update_manager.py").is_file()
    assert (destination / "docs/schema-baseline.json").is_file()
    assert not (destination / "backend/data").exists()
    assert not (destination / "docs/kien-thuc").exists()
    assert (destination / "packaging/solver-patches/solver-after-legacy-guard.py.snapshot").is_file()
    assert not (destination / "dist").exists()
    assert (source / "backend/data/private.json").read_text(encoding="utf-8") == "private data\n"
    skipped = {item["path"] for item in report["skipped"]}
    assert {"backend/data/private.json", "docs/kien-thuc/kiem-toan/private.md",
            } <= skipped


def test_public_copy_refuses_existing_destination(tmp_path):
    source = _source_tree(tmp_path)
    destination = tmp_path / "public"
    destination.mkdir()
    with pytest.raises(prepare.PublicSourceError, match="new, nonexistent"):
        prepare.prepare_public_source(source, destination)
