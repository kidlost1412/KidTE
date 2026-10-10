"""T3: verify_catalog_mapping.py phai doc/ghi machine/catalog_mapping.json,
vi do la file DUY NHAT runtime doc. Truoc T3 no doc/ghi
data/company_catalog_mapping.json (legacy) nen ket qua kiem khong bao gio
anh huong app dang chay.
"""
import importlib.util
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

PATH = Path(__file__).resolve().parents[1] / "tools" / "verify_catalog_mapping.py"
SPEC = importlib.util.spec_from_file_location("verify_catalog_mapping_tool", PATH)
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)

from config import MACHINE_CATALOG_MAPPING_PATH  # noqa: E402


def _source_fixture(tmp_path, date="2026-01-01"):
    path = tmp_path / "data" / "sources" / f"company_website_profiles_{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "products": [
            {"website_product_id": "1", "name": "Trichoderma diet nam benh",
             "packages": ["1kg"]},
            {"website_product_id": "2", "name": "Roots bung re dac biet",
             "packages": ["250ml"]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _machine_fixture(tmp_path, assignments):
    path = tmp_path / "machine.json"
    path.write_text(json.dumps({
        "schema_version": 1, "created_at": "x", "machine_scope": "local_only_do_not_sync",
        "assignments": assignments,
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_default_target_is_the_exact_file_server_py_reads():
    assert TOOL.MACHINE_CATALOG_MAPPING_PATH == MACHINE_CATALOG_MAPPING_PATH


def test_reads_website_product_ids_from_product_code(tmp_path, monkeypatch, capsys):
    _source_fixture(tmp_path)
    machine_path = _machine_fixture(tmp_path, {
        "shop1": {
            "product_code": "WEB-1",
            "listing": {"title": "Trichoderma diet nam benh 1kg"},
            "mapping_audit": {"confidence": "high"},
        },
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(TOOL, "ROOT", tmp_path)
    TOOL.sys.argv = ["verify_catalog_mapping.py", "--date", "2026-01-01",
                      "--machine", str(machine_path)]
    assert TOOL.main() == 0
    out = capsys.readouterr().out
    assert "Đối chiếu 1 ánh xạ" in out
    assert "shop1" in out
    assert "ĐẠT" in out


def test_write_notes_lands_in_mapping_audit_not_top_level(tmp_path, monkeypatch):
    _source_fixture(tmp_path)
    # Tiêu đề SKU KHÔNG chứa từ riêng của hồ sơ web -> phải bị gắn cờ.
    machine_path = _machine_fixture(tmp_path, {
        "shop2": {
            "product_code": "WEB-2",
            "listing": {"title": "San pham khong lien quan gi ca"},
            "mapping_audit": {"confidence": "high"},
        },
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(TOOL, "ROOT", tmp_path)
    TOOL.sys.argv = ["verify_catalog_mapping.py", "--date", "2026-01-01",
                      "--write-notes", "--machine", str(machine_path)]
    assert TOOL.main() == 0

    saved = json.loads(machine_path.read_text(encoding="utf-8"))
    assignment = saved["assignments"]["shop2"]
    assert "review_note" not in assignment  # KHÔNG ghi ở top-level (kiểu cũ)
    assert "Kiểm độc lập" in assignment["mapping_audit"]["review_note"]


def test_regression_tool_and_runtime_share_one_source(tmp_path, monkeypatch):
    """Ghi review_note bằng công cụ trên đúng file --machine == cùng file mà
    server.py truyền cho KnowledgeBase -> đọc lại (như runtime đọc) thấy ngay."""
    from catalog_layers import read_json

    _source_fixture(tmp_path)
    machine_path = _machine_fixture(tmp_path, {
        "shop3": {
            "product_code": "WEB-1",
            "listing": {"title": "San pham khong lien quan gi ca"},
            "mapping_audit": {"confidence": "high"},
        },
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(TOOL, "ROOT", tmp_path)
    TOOL.sys.argv = ["verify_catalog_mapping.py", "--date", "2026-01-01",
                      "--write-notes", "--machine", str(machine_path)]
    assert TOOL.main() == 0

    reloaded = read_json(machine_path)
    assert reloaded["assignments"]["shop3"]["mapping_audit"]["review_note"]
