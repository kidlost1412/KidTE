import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from plugin_registry import discover_plugins, load_manifest, PluginManifestError


def _write_plugin(root, directory, **overrides):
    folder = root / directory
    folder.mkdir(parents=True)
    (folder / "chay.py").write_text("# entry", encoding="utf-8")
    raw = {"id": directory, "ten": directory, "loai": "dich-vu", "phien_ban": "1",
           "can_loi_toi_thieu": "1", "mo_ta": "x", "dung_toi": [], "lenh": ["chay.py"],
           "tu_khoi_dong_lai": False, "kiem_tra_song": "pid", "phu_thuoc_plugin": []}
    raw.update(overrides)
    (folder / "plugin.json").write_text(json.dumps(raw), encoding="utf-8")
    return folder / "plugin.json"


def test_manifest_rejects_missing_type_and_escape(tmp_path):
    missing = _write_plugin(tmp_path, "one")
    data = json.loads(missing.read_text()); data.pop("ten")
    missing.write_text(json.dumps(data), encoding="utf-8")
    try:
        load_manifest(missing)
        raise AssertionError("expected schema error")
    except PluginManifestError as exc:
        assert "Thiếu" in str(exc)
    escaped = _write_plugin(tmp_path, "two", lenh=["../outside.py"])
    try:
        load_manifest(escaped)
        raise AssertionError("expected path error")
    except PluginManifestError as exc:
        assert "thoát" in str(exc)


def test_discovery_isolates_bad_and_duplicate_plugins(tmp_path):
    _write_plugin(tmp_path, "video")
    _write_plugin(tmp_path, "tro-ly-am-thanh")
    duplicate = _write_plugin(tmp_path, "copy", id="video")
    manifests, errors = discover_plugins(tmp_path)
    assert set(manifests) == {"tro-ly-am-thanh"}
    assert {"copy", "video"} <= set(errors)
    assert duplicate.exists()
