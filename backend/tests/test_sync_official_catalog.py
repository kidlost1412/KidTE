"""T3: sync_official_catalog.py phai doc anh xa tu machine/catalog_mapping.json
va xuat ban qua catalog_layers vao shared/product_knowledge.json +
machine/catalog_mapping.json — khong con ghi data/approved_product_knowledge.json
(file legacy runtime khong doc).

Kem theo chot an toan moi: TU CHOI xuat ban neu mot ban ghi LIVE sinh ra
truong (vd canh_bao_quy_cach) ma machine schema hom nay chua co cho chua —
tranh am tham danh mat canh bao quy cach cho toi khi T4 mo rong schema.
"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(TOOLS))

PATH = TOOLS / "sync_official_catalog.py"
SPEC = importlib.util.spec_from_file_location("sync_official_catalog_tool", PATH)
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)

from config import MACHINE_CATALOG_MAPPING_PATH, SHARED_PRODUCT_KNOWLEDGE_PATH  # noqa: E402


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _fixture(tmp_path, *, packages):
    """products/live/machine/shared toi thieu cho mot SKU khong combo.
    `packages` la quy cach cua ho so web — [] de tranh sinh quy_cach_dong_goi/
    canh_bao_quy_cach (nhanh thanh cong); truyen ["1kg"] de kich hoat chot an
    toan (nhanh tu choi)."""
    source_path = tmp_path / "source.json"
    _write(source_path, {"products": [{
        "website_product_id": "9001", "name": "SP Test An Toan",
        "packages": packages, "ingredients": ["N 10%"], "uses": ["Test use"],
        "directions": ["Pha 10g cho 16 lít nước"], "notes": [],
        "url": "https://example.com/sp-test", "source_modified": "2026-09-01",
        "categories": [], "featured_image_url": "", "featured_image_alt": "",
    }]})
    live_path = tmp_path / "live.json"
    _write(live_path, {"products": [{
        "product_id": "555000111", "title": "SP Test An Toan Khong Quy Cach O Ten",
        "original_price": "100.000₫", "price": "90.000₫", "sale_price": "90.000₫",
        "promotion": "", "bag_index": 1, "sku_id": "", "skus": [], "stock_num": 10,
        "is_pinned": False,
    }]})
    machine_path = tmp_path / "machine.json"
    _write(machine_path, {
        "schema_version": 1, "created_at": "2026-09-13T00:00:00+00:00",
        "machine_scope": "local_only_do_not_sync",
        "assignments": {"555000111": {
            "product_code": "WEB-9001", "approved_for_live": False, "approval": {},
            "listing": {"title": "cu"}, "mapping_audit": {"confidence": "high"},
        }},
    })
    shared_path = tmp_path / "shared.json"
    _write(shared_path, {
        "schema_version": 1, "published_at": "2026-09-01T00:00:00+00:00",
        "publisher": "test", "content": {"nhom_san_pham": []}, "content_sha256": "",
    })
    return source_path, live_path, machine_path, shared_path


def _run(source_path, live_path, machine_path, shared_path, *, extra_args=()):
    TOOL.sys.argv = [
        "sync_official_catalog.py", "--date", "2026-01-01",
        "--source", str(source_path), "--live", str(live_path),
        "--machine", str(machine_path), "--shared", str(shared_path),
        *extra_args,
    ]
    with mock.patch.object(
        TOOL, "create_operational_backup",
        return_value={"backup_dir": "khong-that-trong-test"},
    ):
        return TOOL.main()


def test_default_targets_are_the_exact_files_server_py_reads():
    assert TOOL.MACHINE_CATALOG_MAPPING_PATH == MACHINE_CATALOG_MAPPING_PATH
    assert TOOL.SHARED_PRODUCT_KNOWLEDGE_PATH == SHARED_PRODUCT_KNOWLEDGE_PATH


def test_publishes_to_shared_and_machine_not_legacy_file(tmp_path):
    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=[])

    exit_code = _run(source_path, live_path, machine_path, shared_path)

    assert exit_code == 0
    legacy_path = tmp_path / "approved_product_knowledge.json"
    assert not legacy_path.exists(), "khong duoc ghi file legacy nao ca"

    shared = json.loads(shared_path.read_text(encoding="utf-8"))
    machine = json.loads(machine_path.read_text(encoding="utf-8"))
    web_records = shared["content"]["nhom_san_pham"][0]["san_pham"]
    assert any(item["san_pham_id"] == "WEB-9001" for item in web_records)
    assert machine["assignments"]["555000111"]["product_code"] == "WEB-9001"
    assert machine["assignments"]["555000111"]["listing"]["title"] == (
        "SP Test An Toan Khong Quy Cach O Ten"
    )


def test_profile_overrides_survive_publish_since_t4(tmp_path):
    """`[T4]` Ho so web co quy cach "1kg" trong khi SKU LIVE khong ghi quy
    cach nao trong ten -> live_record() sinh quy_cach_dong_goi. Truoc T4,
    machine schema khong co cho chua nen cong cu TU CHOI xuat ban. Tu T4,
    truong nay co nha o assignment["profile_overrides"] nen phai xuat ban
    THANH CONG va giu dung gia tri."""
    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=["1kg"])

    exit_code = _run(source_path, live_path, machine_path, shared_path)

    assert exit_code == 0
    machine = json.loads(machine_path.read_text(encoding="utf-8"))
    overrides = machine["assignments"]["555000111"]["profile_overrides"]
    assert overrides["quy_cach_dong_goi"] == "1kg"


def test_refuses_to_publish_when_a_field_would_be_lost(tmp_path, monkeypatch):
    """Chot tu kiem sau T4: neu profile_overrides KHONG khop voi truong
    live_record() da tinh (mo phong ai do them truong moi ma quen cap nhat
    catalog_layers.PROFILE_OVERRIDE_FIELDS), cong cu phai TU CHOI ghi."""
    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=["1kg"])
    machine_before = machine_path.read_text(encoding="utf-8")
    shared_before = shared_path.read_text(encoding="utf-8")

    import catalog_layers
    monkeypatch.setattr(catalog_layers, "PROFILE_OVERRIDE_FIELDS", ())
    exit_code = _run(source_path, live_path, machine_path, shared_path)

    assert exit_code == 1
    assert machine_path.read_text(encoding="utf-8") == machine_before
    assert shared_path.read_text(encoding="utf-8") == shared_before


def test_regression_tool_and_runtime_share_one_source(tmp_path):
    """sync ghi vao dung --machine/--shared -> doc lai bang read_json (cach
    catalog_layers/server.py doc) phai thay ngay, khong can dong bo gi them."""
    from catalog_layers import read_json

    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=[])
    assert _run(source_path, live_path, machine_path, shared_path) == 0

    machine = read_json(machine_path)
    shared = read_json(shared_path)
    assert machine["assignments"]["555000111"]["product_code"] == "WEB-9001"
    web = shared["content"]["nhom_san_pham"][0]["san_pham"][0]
    assert web["huong_dan_su_dung"] == ["Pha 10g cho 16 lít nước"]


def test_curated_dose_rules_survive_a_resync(tmp_path):
    """dose_rules la truong SHARED do nguoi soan them (vd TC01). Phai doc lai
    tu shared/product_knowledge.json HIEN TAI (khong phai file legacy) va
    giu nguyen qua lan dong bo tiep theo."""
    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=[])
    _write(shared_path, {
        "schema_version": 1, "published_at": "2026-09-01T00:00:00+00:00",
        "publisher": "test", "content": {"nhom_san_pham": [
            {"nhom": "x", "managed_by": "shared_product_knowledge", "san_pham": [
                {"san_pham_id": "WEB-9001", "ten_san_pham": "cu",
                 "dose_rules": [{"cay": "ot", "ml": 30}]},
            ]},
        ]}, "content_sha256": "",
    })

    assert _run(source_path, live_path, machine_path, shared_path) == 0

    shared = json.loads(shared_path.read_text(encoding="utf-8"))
    web = next(item for item in shared["content"]["nhom_san_pham"][0]["san_pham"]
               if item["san_pham_id"] == "WEB-9001")
    assert web["dose_rules"] == [{"cay": "ot", "ml": 30}]


def test_approve_high_flag_still_works_end_to_end(tmp_path):
    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=[])

    assert _run(source_path, live_path, machine_path, shared_path,
                extra_args=("--approve-high",)) == 0

    machine = json.loads(machine_path.read_text(encoding="utf-8"))
    assert machine["assignments"]["555000111"]["approved_for_live"] is True
    assert "lệnh --approve-high" in machine["assignments"]["555000111"]["approval"]["approved_by"]


def test_refuses_and_writes_nothing_when_build_catalog_layers_rejects_ambiguity(
    tmp_path, monkeypatch, capsys,
):
    """`[T5]` build_catalog_layers() có thể từ chối publish (assignment đang
    duyệt mà vẫn mơ hồ — xem test_catalog_layers.py). sync_official_catalog.py
    phải bắt CatalogLayerError, in báo cáo gọn, KHÔNG ghi gì cả — không để lộ
    traceback thô ra terminal của người vận hành."""
    from catalog_layers import CatalogLayerError

    source_path, live_path, machine_path, shared_path = _fixture(tmp_path, packages=[])
    machine_before = machine_path.read_text(encoding="utf-8")
    shared_before = shared_path.read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise CatalogLayerError("TỪ CHỐI PUBLISH — giả lập test T5")

    monkeypatch.setattr(TOOL, "build_catalog_layers", _boom)
    exit_code = _run(source_path, live_path, machine_path, shared_path)

    assert exit_code == 1
    assert "TỪ CHỐI PUBLISH" in capsys.readouterr().out
    assert machine_path.read_text(encoding="utf-8") == machine_before
    assert shared_path.read_text(encoding="utf-8") == shared_before
