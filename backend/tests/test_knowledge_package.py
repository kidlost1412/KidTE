import json
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from knowledge_package import apply_package, content_hash, read_source


def package(version="2"):
    content = {"nhom_san_pham": []}
    return {"schema_version": 1, "version": version, "published_at": "2026-09-13T00:00:00Z",
            "content_sha256": content_hash(content), "content": content}


def test_apply_is_atomic_and_keeps_one_previous(tmp_path):
    target = tmp_path / "shared" / "product_knowledge.json"
    target.parent.mkdir()
    target.write_text('{"old":true}', encoding="utf-8")
    result = apply_package(json.dumps(package()).encode(), target)
    assert result["previous_available"] is True
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == "2"
    assert (target.parent / ".previous" / target.name).read_text(encoding="utf-8") == '{"old":true}'


def test_bad_hash_and_live_are_blocked_without_touching_target(tmp_path):
    target = tmp_path / "knowledge.json"
    target.write_text("old", encoding="utf-8")
    broken = package(); broken["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        apply_package(json.dumps(broken).encode(), target)
    with pytest.raises(RuntimeError, match="LIVE"):
        apply_package(json.dumps(package()).encode(), target, live_running=True)
    assert target.read_text(encoding="utf-8") == "old"


def test_source_rejects_insecure_protocol():
    with pytest.raises(ValueError, match="HTTPS"):
        read_source("http://example.test/knowledge.json")
