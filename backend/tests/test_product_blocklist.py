"""Sản phẩm bị chủ shop loại khỏi AI không lọt vào danh mục live."""
import json

import product_blocklist


def test_title_and_id_filter(tmp_path, monkeypatch):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"title_contains": ["Cá Hồi Na Uy"], "product_ids": ["9"]}), encoding="utf-8")
    monkeypatch.setattr(product_blocklist, "PATH", path)
    items = [{"product_id": "1", "title": "Phân Bón Đạm Cá Hồi Na Uy 500ml"},
             {"product_id": "2", "title": "Đạm Cá Hữu Cơ Amino (Can 5 Lít)"},
             {"product_id": "9", "title": "Khác"}]
    assert [p["product_id"] for p in product_blocklist.filter_products(items)] == ["2"]


def test_missing_file_keeps_all(tmp_path, monkeypatch):
    monkeypatch.setattr(product_blocklist, "PATH", tmp_path / "none.json")
    assert len(product_blocklist.filter_products([{"title": "a"}, {"title": "b"}])) == 2
