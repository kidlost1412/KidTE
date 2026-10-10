import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import runtime_layout


def test_fresh_install_starts_empty_and_never_touches_user_data(tmp_path, monkeypatch):
    """Máy mới cài là máy trắng: chỉ tạo thư mục, không chép dữ liệu shop nào."""
    user = tmp_path / "user"
    monkeypatch.setattr(runtime_layout, "IS_FROZEN", True)
    monkeypatch.setattr(runtime_layout, "ROOT", user)
    assert runtime_layout.SEED_PATHS == ()
    assert runtime_layout.prepare_user_layout()["created"] == []
    assert (user / "backups").is_dir() and (user / "data" / "machine").is_dir()
    assert not list((user / "data").rglob("*.json"))
    mine = user / "data" / "thong_tin_shop.json"
    mine.write_text('{"user":true}', encoding="utf-8")
    runtime_layout.prepare_user_layout()
    assert mine.read_text(encoding="utf-8") == '{"user":true}'
